import base64
import unittest

import nacl.signing

from hub.solana import SolanaWallet, b58decode, b58encode


class SolanaTests(unittest.TestCase):
    def setUp(self):
        self.sk = nacl.signing.SigningKey.generate()
        secret = b58encode(bytes(self.sk) + bytes(self.sk.verify_key))      # som Phantom viser den
        self.w = SolanaWallet(secret, opener=lambda req: None)

    def test_key_and_address_like_phantom(self):
        self.assertEqual(self.w.address, b58encode(bytes(self.sk.verify_key)))
        self.assertEqual(b58decode(self.w.address), bytes(self.sk.verify_key))
        with self.assertRaises(ValueError):
            SolanaWallet(b58encode(bytes(self.sk) + bytes(32)), opener=lambda r: None)   # nøgle og adresse passer ikke sammen

    def test_a_jupiter_trade_is_signed_in_the_first_slot(self):
        msg = b"\x80" + bytes(range(200))
        tx = bytes([1]) + bytes(64) + msg                                     # sådan sender Jupiter den: tom signatur + besked
        signed = self.w.sign(tx)
        self.assertEqual(signed[0], 1)
        self.assertEqual(signed[65:], msg)                                   # beskeden er uændret
        self.sk.verify_key.verify(msg, signed[1:65])                         # og signaturen er gyldig for walletens nøgle

    def test_base58_round_trip(self):
        for b in (b"", b"\0\0abc", bytes(range(32))):
            self.assertEqual(b58decode(b58encode(b)), b)


if __name__ == "__main__":
    unittest.main()
