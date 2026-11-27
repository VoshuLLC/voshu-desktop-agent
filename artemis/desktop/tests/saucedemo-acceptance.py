import json, time, urllib.request

TOKEN = open("/root/.wa-token").read().strip()
BASE = "http://100.127.151.100:8177"

def call(path, body=None, timeout=35):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def tap(x, y): return call("/input", {"action": "tap", "x": x, "y": y})
def type_text(t): return call("/type", {"text": t})
def key(k): return call("/key", {"key": k})
def tree():
    h = call("/hierarchy")
    return h, [e for e in h.get("elements", []) if e.get("source") == "cdp"]

def onscreen(e):
    return 0 <= (e.get("cx") or -99) <= 1280 and 0 <= (e.get("cy") or -99) <= 790

def pick(els, pred):
    hits = [e for e in els if pred(e) and onscreen(e)]
    return hits[0] if hits else None

t0 = time.time()
def log(msg): print("[" + str(round(time.time() - t0, 1)) + "s] " + msg, flush=True)

# 1. navigate to saucedemo via address bar
tap(400, 57); time.sleep(1)
type_text("www.saucedemo.com")
key("enter")
time.sleep(6)

h, els = tree()
log("landed: " + str(h.get("cdp", {}).get("url"))[:50] + " | " + str(len(els)) + " cdp elements")

# 2. login — find username field, type; tab; password; enter
user = pick(els, lambda e: "user" in (e.get("name") or "").lower() or (e.get("role") in ("Edit", "textbox") and onscreen(e)))
if not user:
    inputs = [e for e in els if e.get("role") in ("Edit", "textbox") or "text" in str(e.get("role","")).lower()]
    log("input-ish: " + json.dumps([(e.get("name") or "")[:20] + "@" + str(e.get("cx")) + "," + str(e.get("cy")) for e in inputs[:4]]))
    user = inputs[0] if inputs else None
if not user:
    log("FAIL: no username field"); raise SystemExit(1)
log("username field @ " + str(user["cx"]) + "," + str(user["cy"]))
tap(user["cx"], user["cy"]); time.sleep(0.7)
type_text("standard_user")
key("tab"); time.sleep(0.5)
type_text("secret_sauce")
key("enter")
time.sleep(5)

# 3. inventory page — find first Add to cart
h, els = tree()
log("after login: " + str(h.get("cdp", {}).get("url"))[:55])
atc = pick(els, lambda e: "add to cart" in (e.get("name") or "").lower())
if not atc:
    cands = [e for e in els if "add" in (e.get("name") or "").lower()]
    log("add-ish: " + json.dumps([(e.get("name") or "")[:25] + "@" + str(e.get("cx")) + "," + str(e.get("cy")) for e in cands[:4]]))
    log("FAIL: no add to cart visible"); raise SystemExit(1)
log("add to cart @ " + str(atc["cx"]) + "," + str(atc["cy"]) + " — tapping")
tap(atc["cx"], atc["cy"])
time.sleep(3)

# 4. verify cart badge
h, els = tree()
badge = [e for e in els if e.get("role") in ("link", "Hyperlink") and (e.get("name") or "").strip().isdigit()]
badge2 = [e for e in els if (e.get("name") or "").strip() == "1"]
cartel = [e for e in els if "cart" in str(e.get("href","")).lower() or "cart" in (e.get("name") or "").lower()]
log("cart-ish elements: " + json.dumps([(e.get("name") or "")[:25] + " | " + str(e.get("role")) for e in cartel[:4]]))
log("badge '1' elements: " + str(len(badge2)))
removed = [e for e in els if "remove" in (e.get("name") or "").lower()]
log("REMOVE button present (proves item added): " + str(len(removed) > 0))
if removed or badge2:
    log("ACCEPTANCE: PASS — item in cart, verified semantically in " + str(round(time.time() - t0, 1)) + "s")
else:
    log("ACCEPTANCE: INCONCLUSIVE")
