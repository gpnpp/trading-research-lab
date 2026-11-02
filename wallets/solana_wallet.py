"""
wallets/solana_wallet.py — ОТДЕЛЬНЫЙ «горячий» Solana-кошелёк бота + свопы через Jupiter.

Почему так, а не «основной Phantom»:
  * У Phantom нет API для ботов. Видео про «автоторговлю через Phantom» на деле используют
    отдельный keypair (приватный ключ лежит у бота), а Phantom служит лишь окном для просмотра:
    ключ импортируют в Phantom как «Import Private Key».
  * Поэтому бот СОЗДАЁТ свой собственный кошелёк (wallets/hot_wallet.json). Ты пополняешь его
    небольшой суммой SOL с основного кошелька и по желанию импортируешь ключ в Phantom,
    чтобы видеть позиции. Основной seed-фразы бот не видит и не должен.

Зависимости: только stdlib (ed25519 по RFC 8032 написан здесь — на Python 3.15 нет колёс solders).
Файл hot_wallet.json хранится открытым текстом: держи на нём только то, что готов потерять.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import urllib.request

WALLET_FILE = pathlib.Path(__file__).parent / "hot_wallet.json"
SOL = "So11111111111111111111111111111111111111112"
RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP = "https://lite-api.jup.ag/swap/v1"

# ---------------------------------------------------------------- base58 / ed25519
_A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58e(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _A[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58d(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _A.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


_p = 2 ** 255 - 19
_d = -121665 * pow(121666, -1, _p) % _p
_q = 2 ** 252 + 27742317777372353535851937790883648493
_I = pow(2, (_p - 1) // 4, _p)


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(_d * y * y + 1, -1, _p) % _p
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _I % _p
    if x & 1 != sign:
        x = _p - x
    return x


_By = 4 * pow(5, -1, _p) % _p
_B = (_recover_x(_By, 0), _By, 1, _recover_x(_By, 0) * _By % _p)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _enc(P):
    zi = pow(P[2], -1, _p)
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _H(m):
    return hashlib.sha512(m).digest()


def _expand(seed):
    h = _H(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def pubkey_from_seed(seed: bytes) -> bytes:
    return _enc(_mul(_expand(seed)[0], _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    A = _enc(_mul(a, _B))
    r = int.from_bytes(_H(prefix + msg), "little") % _q
    R = _enc(_mul(r, _B))
    k = int.from_bytes(_H(R + A + msg), "little") % _q
    return R + ((r + k * a) % _q).to_bytes(32, "little")


# ---------------------------------------------------------------- кошелёк
def create_wallet(force: bool = False) -> str:
    """Создаёт новый кошелёк. Существующий НЕ перезаписывает (потеря ключа = потеря денег)."""
    if WALLET_FILE.exists() and not force:
        return load_address()
    seed = os.urandom(32)
    pub = pubkey_from_seed(seed)
    WALLET_FILE.write_text(json.dumps({
        "address": b58e(pub),
        # формат, который понимает Phantom (Import Private Key): base58 от 64 байт seed+pub
        "private_key_base58": b58e(seed + pub),
        "warning": "Никому не показывай. Держи на кошельке только сумму, которую готов потерять.",
    }, indent=2))
    return b58e(pub)


def _seed() -> bytes:
    return b58d(json.loads(WALLET_FILE.read_text())["private_key_base58"])[:32]


def load_address() -> str:
    return json.loads(WALLET_FILE.read_text())["address"]


def _post(url, body, timeout=30):
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"content-type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout) as r:
        return json.load(r)


def rpc(method, params):
    d = _post(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def sol_balance() -> float:
    return rpc("getBalance", [load_address()])["value"] / 1e9


# ---------------------------------------------------------------- Jupiter
def quote(inp: str, out: str, amount: int, slippage_bps: int = 300) -> dict:
    return _get(f"{JUP}/quote?inputMint={inp}&outputMint={out}&amount={amount}&slippageBps={slippage_bps}")


def roundtrip_check(mint: str, sol_lamports: int = 50_000_000) -> dict:
    """Pre-trade проверка «можно ли продать»: котировка покупки, затем котировка продажи купленного.
    Потеря в круге >25% (налоги/огромный спред/нет маршрута) => токен не торгуем.
    ВАЖНО: это проверка маршрута, а не симуляция контракта; полный honeypot она не исключает."""
    try:
        buy = quote(SOL, mint, sol_lamports)
        out_amt = int(buy["outAmount"])
        sell = quote(mint, SOL, out_amt)
        back = int(sell["outAmount"])
    except Exception as e:  # noqa: BLE001
        return dict(ok=False, reason=f"нет маршрута покупки/продажи: {e}")
    loss = (1 - back / sol_lamports) * 100
    impact = float(buy.get("priceImpactPct") or 0) * 100
    return dict(ok=loss <= 25 and impact <= 5, roundtrip_loss_pct=round(loss, 2), price_impact_pct=round(impact, 3),
                reason="" if loss <= 25 and impact <= 5 else f"потеря в круге {loss:.1f}%, импакт {impact:.2f}%")


def swap(inp: str, out: str, amount: int, slippage_bps: int = 300) -> str:
    """РЕАЛЬНЫЙ своп: подписывает и отправляет транзакцию. Вызывать только из solana_trader при LIVE."""
    q = quote(inp, out, amount, slippage_bps)
    s = _post(f"{JUP}/swap", {"quoteResponse": q, "userPublicKey": load_address(), "wrapAndUnwrapSol": True,
                              "dynamicComputeUnitLimit": True,
                              "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"maxLamports": 1_000_000, "priorityLevel": "high"}}})
    raw = base64.b64decode(s["swapTransaction"])
    # VersionedTransaction: compact-u16 число подписей (1 байт при <128), затем 64*n байт, затем message
    n = raw[0]
    message = raw[1 + 64 * n:]
    sig = sign(_seed(), message)
    signed = bytes([n]) + sig + raw[1 + 64:] if n == 1 else None
    if signed is None:
        raise RuntimeError("неожиданное число подписей в транзакции")
    return rpc("sendTransaction", [base64.b64encode(signed).decode(),
                                   {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])


def token_balance(mint: str) -> int:
    r = rpc("getTokenAccountsByOwner", [load_address(), {"mint": mint}, {"encoding": "jsonParsed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in r["value"])


if __name__ == "__main__":
    # самопроверка ed25519 по тестовому вектору RFC 8032 (TEST 1)
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert pubkey_from_seed(seed).hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    assert sign(seed, b"").hex().startswith("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155")
    assert b58d(b58e(b"\0\0abc")) == b"\0\0abc"
    print("ed25519/base58 OK")
