"""Solana: Nova AI's egen wallet (SOLANA_PRIVATE_KEY i /etc/nova-ai.env) og handel med memecoins via Jupiter.

* Saldo: SOL + alle tokens i walleten, regnet om med Jupiters kurser i USD.
* Køb/salg: Jupiter finder den bedste pris; handlen underskrives her på serveren med walletens nøgle og sendes til Solana.
* Betaling sker i USDC. Der skal ligge lidt SOL i walleten til netværksgebyrer.
Nøglen forlader aldrig serveren; kun den underskrevne handel sendes ud.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request

RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
JUP = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"      # tjekket mod blockchainen og Jupiters tokenliste
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")   # tjekket mod blockchainen
SLIPPAGE_BPS = 300          # højst 3 % slip på en memecoin-handel
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def b58encode(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def load_secret(raw: str) -> bytes:
    """Phantom giver nøglen i base58 (64 bytes); nogle værktøjer giver en JSON-liste med tal."""
    raw = raw.strip()
    sk = bytes(json.loads(raw)) if raw.startswith("[") else b58decode(raw)
    if len(sk) not in (32, 64):
        raise ValueError("SOLANA_PRIVATE_KEY har ikke den rigtige længde")
    return sk


class SolanaWallet:
    venue = "Solana"

    def __init__(self, secret: str | None = None, opener=None):
        import nacl.signing                                   # installeret som python3-nacl
        sk = load_secret(secret if secret is not None else os.environ.get("SOLANA_PRIVATE_KEY", ""))
        self.signer = nacl.signing.SigningKey(sk[:32])
        self.address = b58encode(bytes(self.signer.verify_key))
        if len(sk) == 64 and sk[32:] != bytes(self.signer.verify_key):
            raise ValueError("SOLANA_PRIVATE_KEY passer ikke sammen med sin egen adresse")
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=30))
        self._decimals: dict[str, int] = {USDC_MINT: 6, SOL_MINT: 9}

    # ---------- net ----------
    def _http(self, url: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "nova-ai"},
                                     method="POST" if body is not None else "GET")
        with self.open(req) as r:
            return json.loads(r.read().decode())

    def rpc(self, method: str, params: list):
        res = self._http(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if res.get("error"):
            raise RuntimeError(f"Solana {method}: {res['error'].get('message')}")
        return res["result"]

    # ---------- saldo ----------
    def tokens(self) -> dict[str, float]:
        """{mint: antal} for alle tokens i walleten, plus SOL."""
        out = {SOL_MINT: self.rpc("getBalance", [self.address])["value"] / 1e9}
        for prog in TOKEN_PROGRAMS:
            res = self.rpc("getTokenAccountsByOwner", [self.address, {"programId": prog}, {"encoding": "jsonParsed"}])
            for acc in res.get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                amt = info["tokenAmount"]
                self._decimals[info["mint"]] = int(amt["decimals"])
                if float(amt.get("uiAmount") or 0) > 0:
                    out[info["mint"]] = out.get(info["mint"], 0.0) + float(amt["uiAmount"])
        return out

    def prices(self, mints) -> dict[str, float]:
        mints = sorted(set(mints) | {USDC_MINT})
        out = {}
        for i in range(0, len(mints), 50):
            res = self._http(f"{JUP}/price/v3?ids={','.join(mints[i:i + 50])}")
            out.update({m: float(v["usdPrice"]) for m, v in res.items() if v and v.get("usdPrice")})
        out.setdefault(USDC_MINT, 1.0)
        return out

    def value_usd(self) -> dict:
        tok = self.tokens()
        px = self.prices(tok)
        per = {m: round(q * px.get(m, 0.0), 2) for m, q in tok.items()}
        return {"address": self.address, "usd": round(sum(per.values()), 2), "sol": tok.get(SOL_MINT, 0.0),
                "usdc": tok.get(USDC_MINT, 0.0), "tokens": per}

    # ---------- handel ----------
    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            info = self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
            self._decimals[mint] = int(info["data"]["parsed"]["info"]["decimals"])
        return self._decimals[mint]

    def sign(self, tx: bytes) -> bytes:
        """Underskriv en Jupiter-handel: første signatur-plads udfyldes med walletens underskrift af beskeden."""
        n = tx[0]                                             # antal signaturer (altid < 128 her, så én byte)
        msg = tx[1 + 64 * n:]
        sig = self.signer.sign(msg).signature
        return bytes([n]) + sig + tx[1 + 64:]

    def swap(self, in_mint: str, out_mint: str, amount_raw: int) -> dict:
        """Byt `amount_raw` (mindste enheder) af in_mint til out_mint. Returnerer {"in", "out", "sig"} i hele enheder."""
        q = self._http(f"{JUP}/swap/v1/quote?inputMint={in_mint}&outputMint={out_mint}&amount={int(amount_raw)}&slippageBps={SLIPPAGE_BPS}")
        if "outAmount" not in q:
            raise RuntimeError(f"Jupiter: ingen pris ({q.get('error') or q})")
        sw = self._http(f"{JUP}/swap/v1/swap", {"quoteResponse": q, "userPublicKey": self.address, "wrapAndUnwrapSol": True,
                                                "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"})
        tx = self.sign(base64.b64decode(sw["swapTransaction"]))
        sig = self.rpc("sendTransaction", [base64.b64encode(tx).decode(), {"encoding": "base64", "maxRetries": 3}])
        for _ in range(30):                                   # vent på bekræftelse (højst ca. 60 s)
            st = self.rpc("getSignatureStatuses", [[sig]])["value"][0]
            if st and st.get("err"):
                raise RuntimeError(f"Solana-handlen fejlede: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                break
            time.sleep(2)
        else:
            raise RuntimeError("Solana-handlen blev ikke bekræftet i tide")
        return {"in": int(q["inAmount"]) / 10 ** self.decimals(in_mint), "out": int(q["outAmount"]) / 10 ** self.decimals(out_mint), "sig": sig}

    # ---------- som marked i Kassen (samme form som Binance) ----------
    def filters(self) -> dict:
        class Any(dict):
            def __contains__(self, k):
                return True

            def __getitem__(self, k):
                return {"step": 0.0, "min": 1.0, "base": k}
        return Any()

    def open_pos(self, mint: str, side: str, usd: float, lev: float, px: float) -> dict:
        r = self.swap(USDC_MINT, mint, int(usd * 1e6))
        return {"qty": r["out"], "usd": r["in"], "id": r["sig"]}

    def close_pos(self, mint: str, pos: dict, px: float) -> dict:
        held = self.tokens().get(mint, 0.0)
        qty = min(pos["qty"], held)
        r = self.swap(mint, USDC_MINT, int(qty * 10 ** self.decimals(mint)))
        return {"qty": r["in"], "usd": r["out"], "id": r["sig"]}
