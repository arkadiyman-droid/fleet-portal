"""tests/test_directory.py — lining the violations portal's properties up with
this portal's, and working out who should see what (pure), plus the
Cloudflare Access token check behind sign-in by company email."""
import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import cf_access, directory as d


# ── addresses and names ───────────────────────────────────────────────────────

def test_a_street_reads_the_same_however_it_is_written():
    assert d.street("46 South 9TH St aka 451 Wythe Ave") == d.street("50 South 9 St") == d.street("12 S. 9th Street") == "s 9"
    assert d.street("101 Terrace Ave") == d.street("97-99-101 Terrace St") == "terrace"
    assert d.street("650 Gates Avenue (a.k.a 291 Marcus Garvey Blvd)") == d.street("590-600 Gates Ave") == "gates"
    assert d.street("Ind-1 85 Taylor N") == d.street("85 Taylor Street  A".replace("  A", "")) == "taylor"
    assert d.street("126 - 130 Clymer Street") == d.street("Ind-2 130 Clymer S") == "clymer"
    assert d.street("1756 Park Place") == d.street("1756 Park pl") == "park"
    assert d.street("114 E 104th St") == "e 104" and d.street("635 Morris Ave NW Zone") == "morris"
    assert d.street("RP 10AH") == "" and d.street("") == "" and d.street(None) == ""


def test_the_distinctive_words_of_a_property_name():
    assert d.name_tokens("Baisley Park Gardens") == {"baisley", "park", "gardens"}
    assert d.name_tokens("DCA1") == {"dca"} and d.name_tokens("Morris I, LP") == {"morris"}
    assert d.name_tokens("Berry (NYCHA)") == {"berry"} and d.name_tokens("The Plaza") == {"plaza"}
    assert d.name_tokens("Betty and Medgar") == {"betty", "medgar"}


FLEET = [
    {"id": 1, "name": "Berry", "addresses": ["50 South 9 St", "56 South 10 St", "440 Berry St"]},
    {"id": 2, "name": "Betty and Medgar", "addresses": ["590-600 Gates Ave", "650-660 Gates Ave"]},
    {"id": 3, "name": "Bedell Terrace", "addresses": ["14-16 Bedell St", "125-141 Terrace St"]},
    {"id": 4, "name": "Plaza", "addresses": ["37 New Lots Ave"]},
    {"id": 5, "name": "Williams Plaza", "addresses": ["Wil-1 325 Roebling N", "Wil-2 195 Division N"]},
    {"id": 6, "name": "River Park", "addresses": ["RP 10AH", "RP 10AL"]},
    {"id": 7, "name": "DCA", "addresses": ["18 Monroe St", "1756 Park pl", "161 Putnam Ave"]},
]


def _dir(i, name, *addresses):
    return {"id": i, "name": name, "addresses": list(addresses)}


def test_links_are_suggested_from_the_name_and_the_streets():
    s = d.suggest_links([
        _dir(10, "Berry (NYCHA)", "440 Berry Street aka 73 S 10th Street", "46 South 9TH St"),
        _dir(11, "Betty", "650 Gates Avenue"), _dir(12, "Medgar Evers", "700 Gates Avenue"),
        _dir(13, "Bedell", "101 Terrace Ave", "14 Bedell St"),
        _dir(14, "The Plaza", "23 New Lots Avenue"), _dir(15, "Williams Plaza", "325 Roebling Street"),
        _dir(16, "River Park", "10 Richman Plaza"), _dir(17, "DCA1", "1756 Park Place", "161 Putnam Avenue"),
    ], FLEET)
    assert {k: v["property_id"] for k, v in s.items()} == {10: 1, 11: 2, 12: 2, 13: 3, 14: 4, 15: 5, 16: 6, 17: 7}
    assert s[10]["why"] == "same name, 2 streets in common" and s[10]["confidence"] == "name"
    assert s[16]["why"] == "same name"                      # no street in "RP 10AH": the name carries it
    assert s[12]["confidence"] == "name" and "similar name" in s[12]["why"]


def test_a_match_on_streets_alone_is_only_a_hint_and_a_stranger_gets_nothing():
    s = d.suggest_links([_dir(20, "1st Order", "20 Monroe Street", "200 Putnam Avenue"),
                         _dir(21, "Woodland Springs", "9 Elsewhere Road"),
                         _dir(22, "Park Lake", "5 Lake Dr")], FLEET)
    assert s[20]["property_id"] == 7 and s[20]["confidence"] == "streets"
    assert 21 not in s and 22 not in s                       # "Park" alone does not make it River Park


def test_no_suggestion_when_two_properties_are_equally_likely():
    twins = [{"id": 1, "name": "Gates North", "addresses": ["590 Gates Ave"]},
             {"id": 2, "name": "Gates South", "addresses": ["700 Gates Ave"]}]
    assert d.suggest_links([_dir(30, "Gates", "650 Gates Avenue")], twins) == {}


# ── emails, people, the access plan ───────────────────────────────────────────

def test_an_email_that_cannot_be_used_says_why():
    ok = ("pathsco.com",)
    assert d.email_problem("Pat.Lee@PathsCo.com", ok) == "" and d.norm_email(" Pat.Lee@PathsCo.com ") == "pat.lee@pathsco.com"
    assert d.email_problem("pat@pathsco,com", ok) == "not a valid email address"
    assert d.email_problem("pat@pathco.com", ok) == "not a company address (@pathco.com)"
    assert d.email_problem("", ok) == "no email" and d.email_problem("pat@anywhere.org") == ""


ROWS = [
    {"name": "Betty", "property_id": 2, "pm_name": "Pat Lee", "pm_email": "pat@pathsco.com",
     "rpm_name": "Rae Diaz", "rpm_email": "rae@pathsco.com", "site_email": "betty@pathsco.com"},
    {"name": "Medgar Evers", "property_id": 2, "pm_name": "Sam Wu", "pm_email": "Sam@PathsCo.com",
     "rpm_name": "Rae Diaz", "rpm_email": "rae@pathsco.com", "site_email": None},
    {"name": "Berry (NYCHA)", "property_id": 1, "pm_name": "Pat Lee", "pm_email": "pat@pathsco.com",
     "rpm_name": None, "rpm_email": None, "site_email": None},
    {"name": "Bedell", "property_id": 3, "pm_name": "Typo Tom", "pm_email": "tom@pathsco,com",
     "rpm_name": None, "rpm_email": None, "site_email": None},
    {"name": "Not Linked", "property_id": None, "pm_name": "Nora Unlinked", "pm_email": "nora@pathsco.com",
     "rpm_name": None, "rpm_email": None, "site_email": None},
]


def test_people_are_the_managers_on_linked_properties_one_entry_per_email():
    ppl = d.people(ROWS, ("pathsco.com",))
    assert [p["email"] for p in ppl] == ["rae@pathsco.com", "pat@pathsco.com", "sam@pathsco.com", "tom@pathsco,com"]
    by = {p["email"]: p for p in ppl}
    assert by["rae@pathsco.com"]["property_ids"] == {2} and by["rae@pathsco.com"]["role_words"] == "regional manager"
    assert by["pat@pathsco.com"]["property_ids"] == {1, 2} and by["pat@pathsco.com"]["from"] == ["Betty", "Berry (NYCHA)"]
    assert by["sam@pathsco.com"]["name"] == "Sam Wu"                  # matched whatever the capitals
    assert by["tom@pathsco,com"]["problem"] == "not a valid email address"
    assert "nora@pathsco.com" not in by and "betty@pathsco.com" not in by      # not linked; site offices are opt-in
    assert "betty@pathsco.com" in {p["email"] for p in d.people(ROWS, (), include_site=True)}


def test_the_plan_says_what_approving_each_person_would_change():
    ppl = d.people(ROWS, ("pathsco.com",))
    users = {"pat@pathsco.com": {"id": 7, "active": True}, "sam@pathsco.com": {"id": 8, "active": False},
             "gone@pathsco.com": {"id": 9, "active": True}}
    grants = {7: {1: "directory", 5: "directory", 6: None}, 9: {4: "directory", 3: None}}
    plan = {p["email"]: p for p in d.access_plan(ppl, users, grants)}
    assert plan["rae@pathsco.com"]["state"] == "new" and plan["rae@pathsco.com"]["add"] == [2]
    pat = plan["pat@pathsco.com"]
    assert pat["state"] == "add" and pat["add"] == [2] and pat["user_id"] == 7
    assert pat["drop"] == [5]                                 # shared from the directory, no longer backed by it
    assert 6 not in pat["drop"]                               # given by hand: never offered for removal
    assert plan["sam@pathsco.com"]["state"] == "archived" and plan["tom@pathsco,com"]["state"] == "blocked"
    done = d.access_plan(ppl, users, {7: {1: None, 2: "directory"}})
    assert {p["email"]: p for p in done}["pat@pathsco.com"]["state"] == "ok"
    assert d.orphans(ppl, users, grants) == [{"user_id": 9, "email": "gone@pathsco.com", "drop": [4]}]


# ── the Cloudflare Access token ───────────────────────────────────────────────

TEAM, AUD, NOW = "example.cloudflareaccess.com", "aud-of-this-app", 1_800_000_000


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key, kid="k1"):
    n = key.public_key().public_numbers()
    num = lambda i: _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))
    return {"keys": [{"kid": kid, "kty": "RSA", "alg": "RS256", "n": num(n.n), "e": num(n.e)}]}


def _token(key, kid="k1", alg="RS256", **claims):
    body = {"aud": [AUD], "iss": f"https://{TEAM}", "exp": NOW + 600, "iat": NOW - 5, "email": "Pat.Lee@PathsCo.com"}
    body.update(claims)
    head = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(body).encode())
    sig = key.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64(sig)}"


def test_a_genuine_token_gives_the_email_and_anything_else_gives_nothing():
    key, other = _key(), _key()
    keys = cf_access.parse_keys(_jwks(key))
    assert cf_access.verify(_token(key), TEAM, AUD, keys, NOW) == "pat.lee@pathsco.com"
    assert cf_access.verify(_token(key, aud=AUD), TEAM, AUD, keys, NOW) == "pat.lee@pathsco.com"   # aud as a string
    wrong = {
        "signed by someone else": _token(other),
        "for another application": _token(key, aud=["some-other-app"]),
        "from another team": _token(key, iss="https://evil.cloudflareaccess.com"),
        "expired": _token(key, exp=NOW - 1),
        "not valid yet": _token(key, nbf=NOW + 3600),
        "no email (a service token)": _token(key, email=""),
        "an unknown key": _token(key, kid="k2"),
        "unsigned": _token(key, alg="none"),
    }
    for why, tok in wrong.items():
        assert cf_access.verify(tok, TEAM, AUD, keys, NOW) is None, why
    head, payload, sig = _token(key).split(".")
    forged = _b64(json.dumps({"aud": [AUD], "iss": f"https://{TEAM}", "exp": NOW + 600, "email": "boss@pathsco.com"}).encode())
    assert cf_access.verify(f"{head}.{forged}.{sig}", TEAM, AUD, keys, NOW) is None       # payload swapped under the signature
    for junk in (None, "", "abc", "a.b", "a.b.c", "....", 12345):
        assert cf_access.verify(junk, TEAM, AUD, keys, NOW) is None


def test_keys_are_cached_and_reread_when_a_new_key_id_turns_up(monkeypatch):
    key, calls = _key(), []
    cf_access._cache.update(team=None, at=0.0, keys={})

    def fetch(team):
        calls.append(team)
        return cf_access.parse_keys(_jwks(key, kid=f"k{len(calls)}"))
    assert "k1" in cf_access.keys_for(TEAM, "k1", now=1000, fetch=fetch)
    cf_access.keys_for(TEAM, "k1", now=1500, fetch=fetch)
    assert len(calls) == 1                                    # cached
    cf_access.keys_for(TEAM, "k9", now=1030, fetch=fetch)
    assert len(calls) == 1                                    # an unknown id, but read under a minute ago
    assert "k2" in cf_access.keys_for(TEAM, "k9", now=1100, fetch=fetch) and len(calls) == 2
    cf_access.keys_for(TEAM, "k2", now=1100 + cf_access.KEY_MAX_AGE + 1, fetch=fetch)
    assert len(calls) == 3                                    # stale: read again

    def broken(team):
        raise RuntimeError("offline")
    kept = cf_access.keys_for(TEAM, "k3", now=1100 + 2 * cf_access.KEY_MAX_AGE + 9, fetch=broken)
    assert "k3" in kept                                       # a failed read keeps the keys it had
    assert cf_access.identify(None, TEAM, AUD) is None and cf_access.identify("x.y.z", "", AUD) is None
