import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from hub.server import Throttle, check_public_config, client_ip, make_handler


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ThrottleTests(unittest.TestCase):
    def test_locks_after_five_failures_then_doubles_up_to_the_cap(self):
        c = Clock()
        th = Throttle(now=c)
        for _ in range(4):
            th.failed("1.2.3.4")
        self.assertEqual(th.locked("1.2.3.4"), 0)
        th.failed("1.2.3.4")                                   # 5. forkerte forsøg
        self.assertEqual(th.locked("1.2.3.4"), 60)
        c.t += 61
        self.assertEqual(th.locked("1.2.3.4"), 0)
        th.failed("1.2.3.4")                                   # næste forkerte forsøg: dobbelt så længe
        self.assertEqual(th.locked("1.2.3.4"), 120)
        for _ in range(20):
            th.failed("1.2.3.4")
        self.assertEqual(th.locked("1.2.3.4"), 900)            # højst 15 minutter
        self.assertEqual(th.locked("9.9.9.9"), 0)              # andre adresser er upåvirkede

    def test_a_correct_login_resets(self):
        th = Throttle(now=Clock())
        for _ in range(5):
            th.failed("a")
        th.ok("a")
        self.assertEqual(th.locked("a"), 0)

    def test_memory_stays_bounded_under_a_spray_of_addresses(self):
        th = Throttle(now=Clock())
        for i in range(6000):
            th.failed(f"10.0.{i // 250}.{i % 250}")
        self.assertLess(len(th.state), 6000)


class HelperTests(unittest.TestCase):
    def test_client_ip_uses_the_last_forwarded_address_only_from_the_local_proxy(self):
        self.assertEqual(client_ip("127.0.0.1", "6.6.6.6, 85.1.2.3"), "85.1.2.3")      # det første kan være opfundet af klienten
        self.assertEqual(client_ip("203.0.113.9", "1.1.1.1"), "203.0.113.9")           # udefra ignoreres headeren
        self.assertEqual(client_ip("127.0.0.1", "ikke en adresse; drop"), "127.0.0.1")
        self.assertEqual(client_ip("127.0.0.1", None), "127.0.0.1")

    def test_public_mode_needs_a_long_password_and_binds_locally(self):
        host, err = check_public_config({"HUB_PUBLIC": "1", "HUB_HOST": "0.0.0.0", "HUB_PASSWORD": "kort"})
        self.assertIsNotNone(err)
        host, err = check_public_config({"HUB_PUBLIC": "1", "HUB_HOST": "0.0.0.0", "HUB_PASSWORD": "en-lang-adgangskode"})
        self.assertEqual((host, err), ("127.0.0.1", None))                              # tvinges til kun lokal åbning
        self.assertEqual(check_public_config({"HUB_HOST": "0.0.0.0"}), ("0.0.0.0", None))   # uden offentlig tilstand: uændret


class ServerTests(unittest.TestCase):
    def serve(self, **kw):
        os.environ["HUB_PASSWORD"] = "hemmelig-og-lang"
        try:
            handler = make_handler([], **kw)
        finally:
            del os.environ["HUB_PASSWORD"]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    @staticmethod
    def get(url, pw=None, extra=None):
        h = dict(extra or {})
        if pw is not None:
            h["Authorization"] = "Basic " + base64.b64encode(f"x:{pw}".encode()).decode()
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h))
            return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    def test_repeated_wrong_passwords_lock_out_even_the_right_one(self):
        base = self.serve(throttle=Throttle(max_fails=3, base=60))
        self.assertEqual(self.get(base + "/api/bots")[0], 401)                 # uden kode: bare en forespørgsel om login
        self.assertEqual(self.get(base + "/api/bots")[0], 401)
        for _ in range(3):
            self.assertEqual(self.get(base + "/api/bots", "forkert")[0], 401)
        code, headers = self.get(base + "/api/bots", "hemmelig-og-lang")        # nu er adressen spærret
        self.assertEqual(code, 429)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)

    def test_the_right_password_works_and_resets_the_count(self):
        base = self.serve(throttle=Throttle(max_fails=3, base=60))
        for _ in range(2):
            self.get(base + "/api/bots", "forkert")
        self.assertEqual(self.get(base + "/api/bots", "hemmelig-og-lang")[0], 200)
        for _ in range(2):                                                      # tælleren er nulstillet: to nye fejl låser ikke
            self.get(base + "/api/bots", "forkert")
        self.assertEqual(self.get(base + "/api/bots", "hemmelig-og-lang")[0], 200)

    def test_an_attacker_cannot_dodge_the_lock_by_inventing_forwarded_addresses(self):
        base = self.serve(throttle=Throttle(max_fails=3, base=60))
        for i in range(3):                                                      # nye opdigtede adresser først i headeren, samme rigtige til sidst
            self.get(base + "/api/bots", "forkert", {"X-Forwarded-For": f"6.6.6.{i}, 85.1.2.3"})
        self.assertEqual(self.get(base + "/api/bots", "hemmelig-og-lang", {"X-Forwarded-For": "7.7.7.7, 85.1.2.3"})[0], 429)
        self.assertEqual(self.get(base + "/api/bots", "hemmelig-og-lang", {"X-Forwarded-For": "7.7.7.7, 85.9.9.9"})[0], 200)   # anden rigtig adresse

    def test_passwords_with_danish_letters_work_and_wrong_ones_are_counted(self):
        os.environ["HUB_PASSWORD"] = "blåbærgrød-æøå-123"
        try:
            handler = make_handler([], throttle=Throttle(max_fails=2, base=60))
        finally:
            del os.environ["HUB_PASSWORD"]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        self.assertEqual(self.get(base + "/api/bots", "blåbærgrød-æøå-123")[0], 200)
        self.assertEqual(self.get(base + "/api/bots", "gæt-med-æ")[0], 401)           # før: serveren lukkede forbindelsen uden svar
        self.assertEqual(self.get(base + "/api/bots", "gæt-med-ø")[0], 401)
        self.assertEqual(self.get(base + "/api/bots", "blåbærgrød-æøå-123")[0], 429)   # forkerte gæt med æøå tæller med

    def post_login(self, base, pw, extra=None):
        import http.client, urllib.parse
        u = urllib.parse.urlparse(base)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("POST", "/login", urllib.parse.urlencode({"username": "Nova AI", "password": pw}),
                  {"Content-Type": "application/x-www-form-urlencoded", **(extra or {})})
        r = c.getresponse()
        r.read()
        return r.status, r.getheader("Location"), r.getheader("Set-Cookie")

    def test_the_front_page_shows_a_login_page_instead_of_the_browser_box(self):
        base = self.serve()
        try:
            urllib.request.urlopen(base + "/")
            self.fail("forsiden skal kræve login")
        except urllib.error.HTTPError as e:
            code, body, headers = e.code, e.read().decode(), e.headers
        self.assertEqual(code, 401)
        self.assertIn('name="password"', body)
        self.assertIn('action="/login"', body)
        self.assertNotIn("WWW-Authenticate", headers)                           # ingen grå pop-up
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Nova Bot", body)                                       # intet fra selve appen før login

    def test_login_sets_a_cookie_that_opens_the_app_for_30_days(self):
        base = self.serve()
        code, loc, cookie = self.post_login(base, "hemmelig-og-lang", {"X-Forwarded-Proto": "https"})
        self.assertEqual((code, loc), (303, "/"))
        for part in ("HttpOnly", "SameSite=Lax", "Secure", f"Max-Age={30 * 86400}", "Path=/"):
            self.assertIn(part, cookie)
        token = cookie.split(";")[0]
        self.assertEqual(self.get(base + "/api/bots", extra={"Cookie": token})[0], 200)
        self.assertEqual(self.get(base + "/", extra={"Cookie": token})[0], 200)
        self.assertEqual(self.get(base + "/api/bots", extra={"Cookie": "nova_session=9999999999.forfalsket"})[0], 401)
        self.assertEqual(self.get(base + "/api/bots", extra={"Cookie": token[:-2] + "00"})[0], 401)

    def test_wrong_login_passwords_are_counted_and_lock_the_address(self):
        base = self.serve(throttle=Throttle(max_fails=3, base=60))
        self.assertEqual(self.post_login(base, "forkert")[:2], (303, "/login?fejl=1"))
        self.assertEqual(self.post_login(base, "forkert")[:2], (303, "/login?fejl=1"))
        code, loc, cookie = self.post_login(base, "forkert")                      # tredje: spærret
        self.assertTrue(loc.startswith("/login?vent="), loc)
        code, loc, cookie = self.post_login(base, "hemmelig-og-lang")            # selv den rigtige afvises nu
        self.assertTrue(loc.startswith("/login?vent=") and cookie is None, loc)

    def test_logout_clears_the_cookie_and_a_new_password_ends_old_logins(self):
        from hub.server import check_session, make_session
        base = self.serve()
        import http.client, urllib.parse
        u = urllib.parse.urlparse(base)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("GET", "/logout")
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Location")), (303, "/"))
        self.assertIn("nova_session=;", r.getheader("Set-Cookie"))
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        secret = b"k" * 32
        tok = make_session(secret, "gammel-kode-123", 1_800_000_000)
        self.assertTrue(check_session(tok, secret, "gammel-kode-123", 1_800_000_100))
        self.assertFalse(check_session(tok, secret, "ny-kode-4567890", 1_800_000_100))
        self.assertFalse(check_session(tok, secret, "gammel-kode-123", 1_800_000_000 + 31 * 86400))     # udløbet
        self.assertFalse(check_session(tok, b"x" * 32, "gammel-kode-123", 1_800_000_100))               # anden servernøgle

    def test_security_headers_and_hsts_behind_https(self):
        base = self.serve()
        _, h = self.get(base + "/healthz")
        self.assertEqual((h["X-Frame-Options"], h["Referrer-Policy"], h["X-Content-Type-Options"]), ("SAMEORIGIN", "no-referrer", "nosniff"))
        self.assertIsNone(h["Strict-Transport-Security"])
        _, h = self.get(base + "/healthz", None, {"X-Forwarded-Proto": "https"})
        self.assertIn("max-age", h["Strict-Transport-Security"])


if __name__ == "__main__":
    unittest.main()


class RateLimitAndOpenModeTests(unittest.TestCase):
    def test_rate_limit_allows_normal_use_and_blocks_hammering(self):
        from hub.server import RateLimit
        c = Clock()
        rl = RateLimit(limit=5, window=60, now=c)
        self.assertEqual([rl.allow("a") for _ in range(5)], [0.0] * 5)
        self.assertGreater(rl.allow("a"), 0)                    # den 6. inden for et minut afvises
        self.assertEqual(rl.allow("b"), 0)                      # andre adresser er upåvirkede
        c.t += 61
        self.assertEqual(rl.allow("a"), 0)                      # og efter et minut er alt tilladt igen

    def test_open_mode_is_an_explicit_choice_and_needs_the_flag(self):
        self.assertEqual(check_public_config({"HUB_PUBLIC": "1", "HUB_OPEN": "1", "HUB_HOST": "0.0.0.0"}), ("127.0.0.1", None))
        self.assertIsNotNone(check_public_config({"HUB_PUBLIC": "1", "HUB_HOST": "0.0.0.0"})[1])        # uden flag kræves stadig adgangskode
        host, err = check_public_config({"HUB_PUBLIC": "1", "HUB_OPEN": "1", "HUB_PASSWORD": "kort"})
        self.assertIsNotNone(err)                                                                       # en kort adgangskode er stadig for svag

    def test_public_server_rate_limits_even_without_a_password(self):
        from hub.server import RateLimit
        os.environ.pop("HUB_PASSWORD", None)
        handler = make_handler([], ratelimit=RateLimit(limit=3, window=60))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        codes = []
        for _ in range(5):
            try:
                codes.append(urllib.request.urlopen(base + "/api/bots").status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        self.assertEqual(codes, [200, 200, 200, 429, 429])
