"""Pizzeria en ligne - Flask + paiement MVola.
Lancer :  pip install -r requirements.txt && python app.py
Par défaut MVOLA_MODE=simulation (aucun vrai paiement). Passer à "reel" avec vos clés.
"""
import os, sqlite3, uuid, base64, datetime, requests
from flask import Flask, request, session, redirect, url_for, jsonify, render_template_string

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "changez-moi")

# ---------- Configuration MVola ----------
MODE = os.getenv("MVOLA_MODE", "simulation")            # "simulation" ou "reel"
BASE = os.getenv("MVOLA_BASE_URL", "https://devapi.mvola.mg")  # sandbox ; URL prod fournie par Telma
KEY, SECRET = os.getenv("MVOLA_KEY", ""), os.getenv("MVOLA_SECRET", "")
MERCHANT = os.getenv("MVOLA_MERCHANT_MSISDN", "")       # numéro marchand
PARTNER = os.getenv("MVOLA_PARTNER_NAME", "Pizzeria")
CURRENCY = os.getenv("MVOLA_CURRENCY", "KMF")           # à confirmer avec Telma Comores
CALLBACK = os.getenv("MVOLA_CALLBACK_URL", "http://localhost:5000/mvola/callback")

DEFAUT_MENU = {  # produits de départ (modifiables ensuite depuis /admin)
    1: ("Margherita", "Tomate, mozzarella, basilic", 3000),
    2: ("Reine", "Jambon, champignons, mozzarella", 3800),
    3: ("4 Fromages", "Mozzarella, chèvre, gorgonzola, parmesan", 4200),
    4: ("Poulet BBQ", "Poulet grillé, oignons, sauce BBQ", 4000),
    5: ("Thon", "Thon, olives, oignons, tomate", 3800),
    7: ("Burger Classique", "Bœuf, salade, tomate, sauce maison", 3000),
    8: ("Cheeseburger", "Bœuf, cheddar fondu, cornichons", 3500),
    9: ("Burger Poulet", "Poulet croustillant, salade, mayo", 3800),
    10: ("Double Burger", "Deux steaks, double fromage", 4800),
    6: ("Coca-Cola 33cl", "Boisson fraîche", 500),
    11: ("Jus d'orange", "Pressé, sans sucre ajouté", 1000),
    12: ("Eau minérale", "Bouteille 50cl", 300),
}
DEFAUT_CAT = {1: "pizza", 2: "pizza", 3: "pizza", 4: "pizza", 5: "pizza", 7: "burger", 8: "burger", 9: "burger", 10: "burger", 6: "boisson", 11: "boisson", 12: "boisson"}

# ---------- Base de données ----------
def db():
    c = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pizzeria.db")); c.row_factory = sqlite3.Row; return c

with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS commandes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, telephone TEXT, adresse TEXT,
        articles TEXT, total INTEGER, statut TEXT DEFAULT 'en_attente_paiement',
        mvola_ref TEXT, cree_le TEXT)""")

# ---------- Produits : stockés en base, modifiables depuis /admin ----------
with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS produits(id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, description TEXT,
        prix INTEGER, cat TEXT, actif INTEGER DEFAULT 1, ordre INTEGER DEFAULT 0)""")
    if c.execute("SELECT COUNT(*) FROM produits").fetchone()[0] == 0:
        for k, (pid, (n, d, p)) in enumerate(DEFAUT_MENU.items()):
            c.execute("INSERT INTO produits(id,nom,description,prix,cat,actif,ordre) VALUES(?,?,?,?,?,1,?)", (pid, n, d, p, DEFAUT_CAT[pid], k))

MENU, CAT, TOUS = {}, {}, {}   # MENU/CAT : produits disponibles ; TOUS : tous les produits (admin)
def charger_menu():
    global MENU, CAT, TOUS
    with db() as c: rows = c.execute("SELECT * FROM produits ORDER BY ordre, id").fetchall()
    m, ct, t = {}, {}, {}
    for r in rows:
        t[r["id"]] = (r["nom"], r["description"], r["prix"], r["cat"], r["actif"])
        if r["actif"]: m[r["id"]] = (r["nom"], r["description"], r["prix"]); ct[r["id"]] = r["cat"]
    MENU, CAT, TOUS = m, ct, t

charger_menu()
@app.before_request
def _recharger(): charger_menu()

# ---------- Client MVola ----------
def mvola_token():
    auth = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    r = requests.post(f"{BASE}/token", headers={"Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        data={"grant_type": "client_credentials", "scope": "EXT_INT_MVOLA_SCOPE"}, timeout=20)
    r.raise_for_status(); return r.json()["access_token"]

def _headers(token):
    return {"Authorization": f"Bearer {token}", "Version": "1.0", "X-CorrelationID": str(uuid.uuid4()),
            "UserLanguage": "FR", "UserAccountIdentifier": f"msisdn;{MERCHANT}", "partnerName": PARTNER,
            "Content-Type": "application/json", "Cache-Control": "no-cache", "X-Callback-URL": CALLBACK}

def mvola_payer(commande_id, montant, telephone):
    """Déclenche la demande de paiement ; le client confirme avec son code secret sur son téléphone."""
    if MODE == "simulation":
        return "SIM-" + uuid.uuid4().hex[:10]
    corps = {"amount": str(montant), "currency": CURRENCY,
        "descriptionText": f"Commande pizza {commande_id}",
        "requestingOrganisationTransactionReference": f"CMD{commande_id}",
        "requestDate": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "originalTransactionReference": f"CMD{commande_id}",
        "debitParty": [{"key": "msisdn", "value": telephone}],
        "creditParty": [{"key": "msisdn", "value": MERCHANT}],
        "metadata": [{"key": "partnerName", "value": PARTNER}]}
    r = requests.post(f"{BASE}/mvola/mm/transactions/type/merchantpay/1.0.0/",
                      headers=_headers(mvola_token()), json=corps, timeout=30)
    r.raise_for_status(); return r.json()["serverCorrelationId"]

def mvola_statut(ref):
    if MODE == "simulation":
        return "completed"
    r = requests.get(f"{BASE}/mvola/mm/transactions/type/merchantpay/1.0.0/status/{ref}",
                     headers=_headers(mvola_token()), timeout=20)
    r.raise_for_status(); return r.json().get("status", "pending")   # pending / completed / failed

# ---------- Panier ----------
def panier():
    lignes, total = [], 0
    for pid, qte in session.get("panier", {}).items():
        if int(pid) not in MENU: continue
        nom, _, prix = MENU[int(pid)]
        lignes.append((nom, qte, prix * qte, int(pid))); total += prix * qte
    return lignes, total

@app.post("/ajouter/<int:pid>")
def ajouter(pid):
    if pid in MENU:
        p = session.setdefault("panier", {}); p[str(pid)] = p.get(str(pid), 0) + 1; session.modified = True
    return redirect(url_for("accueil") + f"#p{pid}")

@app.post("/retirer/<int:pid>")
def retirer(pid):
    p = session.get("panier", {})
    if p.get(str(pid), 0) > 1: p[str(pid)] -= 1
    else: p.pop(str(pid), None)
    session.modified = True
    return redirect(url_for("accueil") + f"#p{pid}")

@app.post("/vider")
def vider():
    session.pop("panier", None); return redirect(url_for("accueil"))

# ---------- Pages ----------
@app.get("/")
def accueil():
    lignes, total = panier()
    return rendre(ACCUEIL, menu=MENU, lignes=lignes, total=total, n=sum(session.get('panier', {}).values()),
                   qtes={int(k): v for k, v in session.get('panier', {}).items()},
                   cats=CAT, wa=whatsapp(lignes, total), pied=PIED)

@app.route("/commander", methods=["GET", "POST"])
def commander():
    lignes, total = panier()
    if not lignes: return redirect(url_for("accueil"))
    erreur = None
    if request.method == "POST":
        nom, tel, adr = (request.form.get(k, "").strip() for k in ("nom", "telephone", "adresse"))
        tel = "".join(ch for ch in tel if ch.isdigit())
        if not (nom and adr and len(tel) >= 7):
            erreur = "Remplissez le nom, l'adresse et un numéro MVola valide."
        else:
            with db() as c:
                cur = c.execute("INSERT INTO commandes(nom,telephone,adresse,articles,total,cree_le) VALUES(?,?,?,?,?,?)",
                    (nom, tel, adr, "; ".join(f"{q}x {n}" for n, q, _, _ in lignes), total, datetime.datetime.now().isoformat()))
                cid = cur.lastrowid
            try:
                ref = mvola_payer(cid, total, tel)
                with db() as c: c.execute("UPDATE commandes SET mvola_ref=? WHERE id=?", (ref, cid))
                session.pop("panier", None)
                return redirect(url_for("suivi", cid=cid))
            except Exception as e:
                with db() as c: c.execute("UPDATE commandes SET statut='echec' WHERE id=?", (cid,))
                erreur = f"Le paiement MVola n'a pas pu démarrer ({e}). Réessayez."
    return rendre(COMMANDE, lignes=lignes, total=total, erreur=erreur)

@app.get("/suivi/<int:cid>")
def suivi(cid):
    with db() as c: cmd = c.execute("SELECT * FROM commandes WHERE id=?", (cid,)).fetchone()
    return rendre(SUIVI, cmd=cmd) if cmd else ("Commande introuvable", 404)

@app.get("/api/statut/<int:cid>")
def api_statut(cid):
    with db() as c: cmd = c.execute("SELECT * FROM commandes WHERE id=?", (cid,)).fetchone()
    if cmd and cmd["statut"] == "en_attente_paiement" and cmd["mvola_ref"]:
        try:
            s = mvola_statut(cmd["mvola_ref"])
            nouveau = {"completed": "payee", "failed": "echec"}.get(s)
            if nouveau:
                with db() as c: c.execute("UPDATE commandes SET statut=? WHERE id=?", (nouveau, cid))
                return jsonify(statut=nouveau)
        except Exception:
            pass
    return jsonify(statut=cmd["statut"] if cmd else "inconnu")

@app.route("/mvola/callback", methods=["PUT", "POST"])
def callback():
    """MVola notifie ici le résultat du paiement."""
    d = request.get_json(silent=True) or {}
    ref = d.get("serverCorrelationId") or d.get("objectReference")
    st = {"completed": "payee", "failed": "echec"}.get(str(d.get("transactionStatus", "")).lower())
    if ref and st:
        with db() as c: c.execute("UPDATE commandes SET statut=? WHERE mvola_ref=?", (st, ref))
    return "", 200

# ---------- Images : pizzas dessinées en SVG (aucun fichier image à gérer) ----------
import math
def _pts(n, r, dec=0):
    return [(60 + r * math.cos(dec + i * 2 * math.pi / n), 60 + r * math.sin(dec + i * 2 * math.pi / n)) for i in range(n)]

def pizza_svg(pid):
    if pid == 6:  # canette
        return ('<svg viewBox="0 0 120 120"><rect x="38" y="16" width="44" height="90" rx="9" fill="#d62828"/>'
                '<rect x="38" y="40" width="44" height="26" fill="#fff"/><path d="M38 52c14-9 30 9 44 0" stroke="#d62828" stroke-width="5" fill="none"/>'
                '<rect x="44" y="12" width="32" height="7" rx="3" fill="#c9ced6"/></svg>')
    s = ['<svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="57" fill="#d99a4e"/><circle cx="60" cy="60" r="50" fill="#d6402c"/>'
         '<circle cx="60" cy="60" r="46" fill="#f7d774"/>']
    c = lambda p, r, f: s.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="{r}" fill="{f}"/>')
    if pid == 1:
        for p in _pts(6, 30): c(p, 8, "#fffaf0")
        for p in _pts(5, 16, .5): s.append(f'<ellipse cx="{p[0]:.1f}" cy="{p[1]:.1f}" rx="7" ry="4" fill="#2f8f4e" transform="rotate(35 {p[0]:.1f} {p[1]:.1f})"/>')
    elif pid == 2:
        for p in _pts(7, 31): c(p, 8, "#f08a8a")
        for p in _pts(5, 14, .3): c(p, 5, "#e8dcc8")
    elif pid == 3:
        for i, col in enumerate(["#fffaf0", "#e9c46a", "#7aa6d9", "#f2e8cf"]):
            for p in _pts(2, 26, i * math.pi / 2 + .5): c(p, 9, col)
        c((60, 60), 8, "#fffaf0")
    elif pid == 4:
        for p in _pts(7, 30): s.append(f'<rect x="{p[0]-7:.1f}" y="{p[1]-5:.1f}" width="14" height="10" rx="4" fill="#8b4a2b"/>')
        for p in _pts(5, 15, .4): s.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="6" fill="none" stroke="#9b5de5" stroke-width="2.5"/>')
    elif pid == 5:
        for p in _pts(7, 30): c(p, 7, "#e59f8c")
        for p in _pts(6, 16, .6): s.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="5" fill="#2b2b2b"/><circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="2" fill="#f7d774"/>')
    s.append("</svg>")
    return "".join(s)


def produit_svg(pid):
    cat = TOUS.get(pid, ("", "", 0, "pizza", 1))[3]
    if cat == "burger" and pid not in (7, 8, 9, 10): pid = (7, 8, 9, 10)[pid % 4]
    elif cat == "boisson" and pid not in (6, 11, 12): pid = (6, 11, 12)[pid % 3]
    elif cat == "pizza" and not 1 <= pid <= 5: pid = 1 + pid % 5
    if cat == "burger":
        p = ['<svg viewBox="0 0 120 120"><path d="M16 50a44 32 0 0 1 88 0z" fill="#d98e3d"/>']
        for x, y in ((38, 36), (56, 28), (74, 37), (48, 44), (66, 44)):
            p.append(f'<ellipse cx="{x}" cy="{y}" rx="3.5" ry="1.8" fill="#f7e2b0"/>')
        p.append('<path d="M12 52q8 9 16 0t16 0 16 0 16 0 16 0 16 0v7H12z" fill="#5fae3b"/>')
        if pid in (7, 8, 9): p.append('<rect x="16" y="60" width="88" height="5" rx="2.5" fill="#d23a24"/>')
        pat = "#c98a3a" if pid == 9 else "#6b3a1e"
        ys = (66, 78) if pid == 10 else (68,)
        for y in ys:
            p.append(f'<rect x="14" y="{y}" width="92" height="12" rx="6" fill="{pat}"/>')
            if pid in (8, 10): p.append(f'<path d="M16 {y}h88l-9 9-9-5-9 5-9-5-9 5-9-5-9 5z" fill="#f2c14e"/>')
        yb = 92 if pid == 10 else 82
        p.append(f'<path d="M16 {yb}h88v5a12 12 0 0 1-12 12H28a12 12 0 0 1-12-12z" fill="#d98e3d"/></svg>')
        return "".join(p)
    if pid == 11:
        return ('<svg viewBox="0 0 120 120"><path d="M32 30h56l-6 74H38z" fill="#f4a21c"/><path d="M32 30h56l-1.5 18h-53z" fill="#f8c04a"/>'
                '<rect x="62" y="8" width="5" height="40" rx="2" fill="#d23a24" transform="rotate(12 64 28)"/>'
                '<circle cx="90" cy="34" r="13" fill="#f7931e"/><circle cx="90" cy="34" r="9" fill="#ffd27a"/></svg>')
    if pid == 12:
        return ('<svg viewBox="0 0 120 120"><rect x="42" y="30" width="36" height="76" rx="13" fill="#bfe3f5"/><rect x="51" y="12" width="18" height="16" rx="3" fill="#2a7fb8"/>'
                '<rect x="42" y="56" width="36" height="22" fill="#fff"/><path d="M48 68q6-6 12 0t12 0" stroke="#2a7fb8" stroke-width="3" fill="none"/></svg>')
    return pizza_svg(pid)

# ---------- Images personnalisées et logo (dossier static/uploads) ----------
import glob
from markupsafe import escape
UP = os.path.join(app.root_path, "static", "uploads"); os.makedirs(UP, exist_ok=True)
EXT = {"png", "jpg", "jpeg", "webp"}
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # 5 Mo max par image

def fichier(nom):
    f = glob.glob(os.path.join(UP, nom + ".*"))
    return (url_for("static", filename="uploads/" + os.path.basename(f[0])) + f"?v={int(os.path.getmtime(f[0]))}") if f else None

def visuel(pid):
    u = fichier(f"produit_{pid}")
    return f'<img src="{u}" alt="{escape(TOUS.get(pid, ("Produit",))[0])}" loading="lazy">' if u else produit_svg(pid)

def logo_html():
    u = fichier("logo")
    if u: return f'<img class="logoimg" src="{u}" alt="Ibra Tech">'
    return ('<span class="marque"><svg viewBox="0 0 40 40" width="38" height="38" aria-hidden="true"><circle cx="20" cy="20" r="19" fill="#e0ac2b"/>'
            '<text x="20" y="26" text-anchor="middle" font-size="17" font-weight="800" fill="#082d21" font-family="system-ui">IT</text></svg><b>Ibra Tech</b></span>')


from urllib.parse import quote
WA = os.getenv("WHATSAPP_NUMBER", "4324866")   # numéro WhatsApp sans le + (ex. 4324866)
PIED = os.getenv("PIED", f"Comores • Service client WhatsApp : +{WA} • Livraison 30–40 min")

@app.context_processor
def _ctx():
    return {"nb": sum(session.get("panier", {}).values())}

def whatsapp(lignes, total):
    txt = ("Bonjour, je voudrais commander : " + ", ".join(f"{q}x {n}" for n, q, _, _ in lignes) + f". Total : {total} KMF.") if lignes else "Bonjour, je voudrais commander."
    return f"https://wa.me/{WA}?text={quote(txt)}"

def rendre(corps, **ctx):
    return render_template_string(PAGE.replace("@@CORPS@@", corps), svg=visuel, logo=logo_html(), **ctx)

# ---------- Gabarits ----------
PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b3d2e">
<title>Pizzeria Moroni</title><style>
:root{--or:#d9ab3f;--orvif:#ecc968;--orgrad:linear-gradient(180deg,#f3d67f,#c8982f);--vert:#0b3d2e;--fonce:#062a20;--creme:#f6ecd6;--encre:#1b1606;--motif:repeating-linear-gradient(135deg,rgba(217,171,63,.10) 0 1px,transparent 1px 26px)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--vert);color:var(--creme);padding-bottom:5.5rem}
.app{max-width:1160px;margin:0 auto}
a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid var(--orvif);outline-offset:2px}
.entete{display:flex;justify-content:space-between;align-items:center;margin:.8rem;padding:.8rem 1rem;background:var(--fonce);border-radius:22px;border:1px solid rgba(217,171,63,.35)}
.ico{position:relative;display:grid;place-items:center}.pt{position:absolute;top:-6px;right:-10px;background:var(--or);color:var(--encre);border-radius:99px;font-size:.7rem;font-weight:800;min-width:18px;height:18px;display:grid;place-items:center;padding:0 4px}
svg.i{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.marque{display:inline-flex;align-items:center;gap:.6rem;font-size:1.3rem;color:var(--orvif);font-style:italic;font-weight:700}
.logoimg{height:44px;max-width:180px;object-fit:contain;display:block}
.salut{margin:.8rem;padding:1.2rem;border:1px solid rgba(217,171,63,.55);border-radius:24px;background:var(--fonce) var(--motif)}
.salut h1{margin:0;font-size:1.5rem;color:var(--orvif)}.salut p{margin:.2rem 0 1rem}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:.6rem}
.stat{border:1px solid rgba(217,171,63,.55);border-radius:14px;padding:.7rem .3rem;text-align:center;font-size:.78rem}
.stat svg{width:28px;height:28px;color:var(--orvif)}.stat b{display:block;font-size:.95rem;color:#fff}
.strip{display:flex;justify-content:space-between;align-items:center;margin-top:.9rem;background:rgba(0,0,0,.25);border-radius:14px;padding:.8rem 1rem}
.strip b{font-size:1.05rem;color:#fff}.strip small{display:block}.strip a{color:var(--orvif);font-weight:700;text-decoration:none}
.menu{background:var(--creme);color:var(--encre);border-radius:28px 28px 0 0;padding:1.2rem 1rem 1rem;margin-top:1.2rem}
.menu h2{margin:0 0 .8rem;font-size:1.5rem;display:inline-block;border-bottom:2px solid var(--or);padding-bottom:.2rem}
.filtres{display:flex;gap:.5rem;overflow-x:auto;padding:.2rem 0 .8rem}
.filtres button{border:1px solid var(--vert);background:transparent;color:var(--vert);padding:.5rem 1rem;border-radius:99px;font-weight:600;font-size:.9rem;font-family:inherit;cursor:pointer;white-space:nowrap}
.filtres button.on{background:var(--vert);color:var(--orvif)}
#q{margin-bottom:.9rem;border:1px solid #cdbb85}
.grille{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.8rem}
.carte{background:var(--vert) var(--motif);border:1px solid rgba(217,171,63,.6);border-radius:18px;padding:.7rem;display:flex;flex-direction:column;color:var(--creme);text-align:center}
.carte .img{border-bottom:1px solid rgba(217,171,63,.4);padding-bottom:.5rem;margin-bottom:.5rem}
.carte .img svg,.carte .img img{width:100%;aspect-ratio:1;object-fit:cover;display:block;border-radius:12px}
.carte .img svg{filter:drop-shadow(0 6px 6px rgba(0,0,0,.4))}
.carte h3{margin:0;font-size:1rem;color:#fff}.carte p{margin:.2rem 0 .4rem;font-size:.75rem;opacity:.85;flex:1}
.prix{color:var(--orvif);font-weight:800;font-size:1.05rem;margin-bottom:.5rem}
.ajout{width:100%;border:0;border-radius:10px;padding:.6rem;background:var(--orgrad);color:var(--encre);font-weight:700;font-size:.95rem;font-family:inherit;cursor:pointer}
.qte{display:flex;justify-content:space-between;align-items:center;background:rgba(0,0,0,.3);border-radius:10px;padding:.2rem;font-weight:800;color:#fff}.qte form{margin:0}
.plus,.moins{width:38px;height:34px;border:0;border-radius:8px;background:var(--orgrad);color:var(--encre);font-size:1.2rem;cursor:pointer}.moins{background:var(--creme)}
.wa{display:flex;align-items:center;gap:.8rem;margin-top:1rem;background:var(--vert);color:var(--creme);text-decoration:none;border:1px solid var(--or);border-radius:18px;padding:.8rem}
.wa .ic{width:46px;height:46px;border-radius:12px;background:var(--orgrad);display:grid;place-items:center;flex:none}
.wa b{color:var(--orvif);display:block;font-size:1.05rem}.wa small{font-size:.72rem;opacity:.9}
.wa .pil{margin-left:auto;background:var(--orgrad);color:var(--encre);border-radius:10px;padding:.4rem .6rem;font-size:.75rem;font-weight:700;text-align:center}
.pied{background:var(--creme);color:#4d3b05;font-size:.72rem;text-align:center;padding:.8rem 1rem 6rem}
.nav{position:fixed;left:50%;transform:translateX(-50%);bottom:0;width:min(520px,100%);display:grid;grid-auto-flow:column;grid-auto-columns:1fr;background:var(--fonce);border-top:1px solid rgba(217,171,63,.4);padding-bottom:env(safe-area-inset-bottom,0px);z-index:5}
.nav a{display:flex;flex-direction:column;align-items:center;gap:.2rem;padding:.6rem 0;color:var(--creme);opacity:.85;text-decoration:none;font-size:.72rem;position:relative}
.nav a.on{background:var(--orgrad);color:var(--encre);opacity:1;font-weight:700}.nav svg{width:26px;height:26px}
.haut{display:flex;align-items:center;gap:.8rem;padding:.4rem 1.2rem 1rem}.haut a{text-decoration:none;font-size:1.5rem;color:var(--orvif)}.haut h1{margin:0;font-size:1.3rem;color:var(--orvif)}
.bloc{margin:0 1.2rem 1rem;background:var(--creme);color:var(--encre);border-radius:20px;padding:1rem}
.bloc h2{margin:0 0 .6rem;font-size:1rem}
.art{display:flex;align-items:center;gap:.8rem;padding:.4rem 0}.art svg,.art img{width:46px;height:46px;flex:none;border-radius:12px;object-fit:cover}.art span:nth-child(2){flex:1}
.tot{display:flex;justify-content:space-between;font-weight:800;border-top:1px dashed #b9983a;margin-top:.5rem;padding-top:.7rem}
label{display:block;font-size:.85rem;font-weight:600;margin:.8rem 0 .3rem}
input{width:100%;padding:.85rem;border:2px solid transparent;border-radius:12px;background:#fff;font-size:1rem;font-family:inherit;color:var(--encre)}
input:focus{border-color:var(--vert);outline:none}
.mvola{display:flex;align-items:center;gap:.8rem;background:#fff;border:2px solid var(--vert);border-radius:14px;padding:.8rem;font-size:.9rem}
.mvola b{background:var(--vert);color:var(--creme);border-radius:8px;padding:.35rem .6rem}
.payer{display:block;width:calc(100% - 2.4rem);margin:0 1.2rem;background:var(--orgrad);color:var(--encre);border:0;border-radius:16px;padding:1.05rem;font-weight:700;font-size:1.05rem;font-family:inherit;text-align:center;text-decoration:none;cursor:pointer}
.err{background:#fde8e4;color:#9b1c10;margin:0 1.2rem 1rem;padding:.8rem;border-radius:12px;font-size:.9rem}
.etapes{list-style:none;margin:0;padding:0}.etapes li{display:flex;gap:.8rem;align-items:center;padding:.7rem 0;color:#6b5a1e}
.etapes i{width:26px;height:26px;border-radius:50%;background:#cdbd85;flex:none;display:grid;place-items:center;color:#fff;font-style:normal;font-size:.85rem}
.etapes li.fait{color:var(--encre);font-weight:600}.etapes li.fait i{background:var(--vert)}
.etapes li.cours{color:var(--encre);font-weight:600}.etapes li.cours i{background:var(--or);color:var(--encre);animation:pouls 1.2s infinite}
@keyframes pouls{50%{transform:scale(1.2)}}@media(prefers-reduced-motion:reduce){.etapes li.cours i{animation:none}}
.logo{margin:0}.petit{border:0;border-radius:10px;padding:.6rem .9rem;font-weight:600;font-size:.9rem;font-family:inherit;background:var(--vert);color:var(--orvif);cursor:pointer}
.petit.clair{background:transparent;color:var(--encre);border:1px solid var(--encre)}
.adm{display:flex;gap:.8rem;align-items:center;padding:.6rem 0;border-bottom:1px solid #d9c98d;flex-wrap:wrap}
.adm .vue{width:64px;height:64px;flex:none}.adm .vue svg,.adm .vue img{width:64px;height:64px;object-fit:cover;border-radius:12px}
.adm form{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;margin:0}.adm input[type=file]{width:auto;padding:.4rem;font-size:.85rem}
.adm .nom{flex:1;min-width:120px;font-weight:700}
.prod{border-bottom:1px solid #d9c98d;padding:.8rem 0}.prod:last-child{border-bottom:0}.prod .ligne2{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
.prod .btns{display:flex;gap:.5rem;flex-wrap:wrap;margin:.8rem 0 0}.prod input[type=file]{width:auto;padding:.4rem;font-size:.85rem}
.prod input[type=checkbox]{width:auto;margin-right:.4rem}.case{display:flex;align-items:center}
select{width:100%;padding:.85rem;border:2px solid transparent;border-radius:12px;background:#fff;font-size:1rem;font-family:inherit;color:var(--encre)}
.bloc.prod{border-bottom:0;padding:1rem}

/* --- Version ordinateur + lisibilité --- */
.carte{background:#115740 var(--motif);transition:transform .15s}
.carte .img{background:rgba(255,255,255,.07);border-radius:14px;padding:.5rem}
.carte h3{font-size:1.08rem}.carte p{font-size:.9rem;opacity:.92}
.prix{font-size:1.2rem}.ajout{font-size:1.02rem;padding:.75rem}
.ajout:active,.plus:active,.moins:active{transform:scale(.96)}
.filtres{position:sticky;top:0;z-index:4;background:var(--creme);padding-top:.6rem;margin-top:-.4rem}
.filtres button{font-size:1rem;padding:.55rem 1.1rem}
.stat{font-size:.88rem}.stat b{font-size:1rem}.salut p{font-size:1.05rem}.strip small{font-size:.9rem}
.wa b{font-size:1.12rem}.wa small{font-size:.88rem}.wa .pil{font-size:.85rem}
.pied{font-size:.9rem;color:#3b2d04}.nav a{font-size:.8rem}
.haut{width:100%;max-width:640px;margin-inline:auto}
.bloc,.err{width:calc(100% - 2.4rem);max-width:640px;margin-inline:auto}
.payer{max-width:640px;margin-inline:auto}
@media(min-width:620px){.grille{grid-template-columns:repeat(3,1fr)}}
@media(min-width:900px){
 .grille{grid-template-columns:repeat(4,1fr);gap:1.1rem}
 .salut h1{font-size:1.9rem}.stats{max-width:720px}.entete{margin:1rem .8rem}
 .nav{width:min(560px,calc(100% - 2rem));bottom:1rem;border-radius:18px;border:1px solid rgba(217,171,63,.5);overflow:hidden}}
@media(min-width:1200px){.menu,.pied{box-shadow:0 0 0 100vmax var(--creme);clip-path:inset(0 -100vmax);border-radius:0}}
@media(hover:hover) and (prefers-reduced-motion:no-preference){.carte:hover{transform:translateY(-3px)}}

/* --- Adaptation fluide à toutes les tailles d'écran --- */
html{font-size:clamp(16px,.45vw + 14px,20px)}
.app{max-width:1240px}
.logoimg{height:2.8rem}
.grille{grid-template-columns:repeat(auto-fill,minmax(clamp(150px,18vw,250px),1fr))}
.salut h1{font-size:clamp(1.5rem,2.4vw,2.3rem)}.stats{max-width:none}
.nav{left:0;right:0;transform:none;width:100%;max-width:none;border-radius:0;border:0;border-top:1px solid rgba(217,171,63,.4);bottom:0;overflow:visible;padding-inline:max(0px,calc((100vw - 1240px)/2))}
.nav a{padding:.7rem 0;font-size:clamp(.75rem,.2vw + .7rem,.95rem)}
.nav svg{width:1.7rem;height:1.7rem}
.haut.large{max-width:1000px}.cols{width:100%;max-width:1000px;margin-inline:auto}
@media(min-width:900px){.cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem;align-items:start}}
</style></head><body><div class="app"><header class="entete"><div>{{ logo|safe }}</div></header>
@@CORPS@@</div>
<nav class="nav" aria-label="Navigation">
<a href="/" class="{{ 'on' if request.endpoint=='accueil' }}"><span class="ico"><svg class="i" viewBox="0 0 24 24"><path d="M3 11l9-8 9 8v10h-6v-6H9v6H3z"/></svg></span>Accueil</a>
<a href="/#menu"><span class="ico"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg></span>Menu</a>
<a href="/#q" id="nav-rech"><span class="ico"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg></span>Recherche</a>
<a href="/commander" class="{{ 'on' if request.endpoint in ('commander','suivi') }}"><span class="ico"><svg class="i" viewBox="0 0 24 24"><path d="M3 4h2l2.4 11h10.2L20 7H6"/><circle cx="9" cy="20" r="1.5"/><circle cx="17" cy="20" r="1.5"/></svg>{% if nb %}<span class="pt">{{nb}}</span>{% endif %}</span>Panier</a>
{% if session.get('admin') or request.endpoint=='admin' %}<a href="{{url_for('admin')}}" class="{{ 'on' if request.endpoint=='admin' }}"><span class="ico"><svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/></svg></span>Admin</a>{% endif %}</nav></body></html>"""

ACCUEIL = """<section class="salut"><h1>Bonjour, bienvenue</h1><p>Pizzas, burgers et boissons, livrés à Moroni.</p>
<div class="stats">
<div class="stat"><svg class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span>Livraison</span><b>30–40 min</b></div>
<div class="stat"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 14.5h2"/></svg><span>Paiement</span><b>MVola</b></div>
<div class="stat"><svg class="i" viewBox="0 0 24 24"><path d="M12 21s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="9" r="2.5"/></svg><span>Zone</span><b>Moroni</b></div></div>
<div class="strip"><div><b>Notre menu</b><small>Pizzas, burgers, boissons</small></div><a href="#menu">Voir tout ›</a></div></section>
<section class="menu" id="menu"><h2>Produits populaires</h2>
<div class="filtres"><button class="on" data-f="">Tout</button><button data-f="pizza">Pizzas</button><button data-f="burger">Burgers</button><button data-f="boisson">Boissons</button></div>
<input id="q" type="search" placeholder="Rechercher un produit" aria-label="Rechercher un produit">
<div class="grille">
{% for pid,(nom,desc,prix) in menu.items() %}
<div class="carte" id="p{{pid}}" data-c="{{cats[pid]}}" data-n="{{nom|lower}}">
<div class="img">{{ svg(pid)|safe }}</div><h3>{{nom}}</h3><p>{{desc}}</p><div class="prix">{{prix}} KMF</div>
{% if qtes.get(pid) %}<div class="qte"><form method="post" action="{{url_for('retirer',pid=pid)}}"><button class="moins" aria-label="Retirer {{nom}}">−</button></form>{{qtes[pid]}}
<form method="post" action="{{url_for('ajouter',pid=pid)}}"><button class="plus" aria-label="Ajouter {{nom}}">+</button></form></div>
{% else %}<form method="post" action="{{url_for('ajouter',pid=pid)}}"><button class="ajout">+ Ajouter</button></form>{% endif %}
</div>{% endfor %}</div>
<a class="wa" href="{{wa}}" target="_blank" rel="noopener"><span class="ic"><svg viewBox="0 0 24 24" width="30" height="30"><path fill="#062a20" d="M12 2a10 10 0 0 0-8.6 15L2 22l5.1-1.3A10 10 0 1 0 12 2zm5.2 14.1c-.2.6-1.2 1.2-1.7 1.2-.5.1-1 .2-3.3-.7-2.8-1.2-4.6-4-4.7-4.2-.1-.2-1.1-1.5-1.1-2.8 0-1.3.7-2 .9-2.3.2-.3.5-.3.7-.3h.5c.2 0 .4 0 .6.5l.8 1.9c.1.2.1.4 0 .6l-.3.5-.4.4c-.1.2-.3.3-.1.6.2.3.8 1.3 1.7 2.1 1.2 1 2.1 1.3 2.4 1.5.3.1.5.1.6-.1l.9-1.1c.2-.3.4-.2.6-.1l1.8.9c.3.1.5.2.5.3.1.2.1.8-.1 1.4z"/></svg></span>
<span><b>Commander via WhatsApp</b><small>Commande rapide · Livraison à Moroni</small></span>
<span class="pil">{% if n %}Panier : {{n}} produit{{'s' if n>1}}<br>{{total}} KMF{% else %}Panier vide{% endif %}</span></a></section>
<div class="pied">{{pied}}</div>
<script>let cat="";const q=document.getElementById("q");
function f(){const t=q.value.trim().toLowerCase();document.querySelectorAll(".carte").forEach(c=>c.style.display=((!cat||c.dataset.c===cat)&&c.dataset.n.includes(t))?"":"none")}
document.querySelectorAll(".filtres button").forEach(b=>b.onclick=()=>{cat=b.dataset.f;document.querySelectorAll(".filtres button").forEach(x=>x.classList.toggle("on",x===b));f()});
q.oninput=f;
function ch(){if(location.hash==='#q'){q.scrollIntoView({block:'center'});q.focus()}}
addEventListener('hashchange',ch);ch();</script>"""

COMMANDE = """<div class="haut large"><h1>Votre commande</h1></div>
<div class="cols"><div><div class="bloc"><h2>Panier</h2>
{% for n,q,s,pid in lignes %}<div class="art">{{ svg(pid)|safe }}<span>{{q}} × {{n}}</span><b>{{s}} KMF</b></div>{% endfor %}
<div class="tot"><span>Total</span><span>{{total}} KMF</span></div></div>
</div><div>{% if erreur %}<div class="err">{{erreur}}</div>{% endif %}
<form method="post" id="f"><div class="bloc"><h2>Livraison</h2>
<label for="nom">Nom</label><input id="nom" name="nom" autocomplete="name" required>
<label for="adresse">Adresse de livraison</label><input id="adresse" name="adresse" autocomplete="street-address" required></div>
<div class="bloc"><h2>Paiement</h2><div class="mvola"><b>MVola</b><span>Vous confirmez avec votre code secret sur votre téléphone.</span></div>
<label for="tel">Numéro MVola</label><input id="tel" name="telephone" inputmode="tel" placeholder="Ex. 325 12 34" required></div>
<button class="payer">Payer {{total}} KMF</button></form></div></div>"""

SUIVI = """<div class="haut"><h1>Commande n° {{cmd['id']}}</h1></div>
<div class="bloc"><p style="margin:0 0 .5rem">{{cmd['articles']}}<br><b>{{cmd['total']}} KMF</b></p>
<ul class="etapes" id="e"><li class="cours"><i>1</i>Confirmez le paiement sur votre téléphone</li>
<li><i>2</i>Votre pizza est préparée</li><li><i>3</i>Livraison en route</li></ul></div>
<a class="payer" href="/">Nouvelle commande</a>
<script>
const L=document.querySelectorAll("#e li");
function etat(s){if(s==="payee"){L[0].className="fait";L[0].innerHTML="<i>✓</i>Paiement reçu";L[1].className="cours";}
else if(s==="echec"){L[0].className="";L[0].innerHTML="<i>!</i>Le paiement a échoué, vous pouvez recommander";}}
async function verifier(){const d=await (await fetch("/api/statut/{{cmd['id']}}")).json();etat(d.statut);
if(d.statut==="en_attente_paiement")setTimeout(verifier,3000);}
verifier();
</script>"""

# ---------- Administration : changer les images et le logo ----------
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")   # CHANGEZ-LE !
ADMIN_PATH = os.getenv("ADMIN_PATH", "admin").strip("/")   # adresse secrète de l'administration

def admin_requis(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if session.get("admin") else redirect(url_for("admin"))
    return w

def enregistrer(nom):
    f = request.files.get("image")
    ext = f.filename.rsplit(".", 1)[-1].lower() if f and "." in f.filename else ""
    if ext in EXT:
        for old in glob.glob(os.path.join(UP, nom + ".*")): os.remove(old)
        f.save(os.path.join(UP, f"{nom}.{ext}"))

@app.route(f"/{ADMIN_PATH}", methods=["GET", "POST"])
def admin():
    erreur = None
    if request.method == "POST":
        if request.form.get("mdp") == ADMIN_PASSWORD: session["admin"] = True
        else: erreur = "Mot de passe incorrect."
    return rendre(ADMIN, produits=TOUS, connecte=session.get("admin"), erreur=erreur, logo_perso=fichier("logo"))

@app.post(f"/{ADMIN_PATH}/image/<nom>")
@admin_requis
def admin_image(nom):
    if nom == "logo" or (nom.startswith("produit_") and nom[8:].isdigit() and int(nom[8:]) in MENU):
        if request.form.get("action") == "reset":
            for old in glob.glob(os.path.join(UP, nom + ".*")): os.remove(old)
        else: enregistrer(nom)
    return redirect(url_for("admin"))


def lire_prod_form():
    nom = request.form.get("nom", "").strip()[:60]
    desc = request.form.get("description", "").strip()[:120]
    prix = "".join(ch for ch in request.form.get("prix", "") if ch.isdigit())
    cat = request.form.get("cat")
    if not nom or not prix or int(prix) <= 0 or cat not in ("pizza", "burger", "boisson"): return None
    return nom, desc, int(prix), cat

@app.post(f"/{ADMIN_PATH}/produit/nouveau")
@admin_requis
def admin_produit_nouveau():
    d = lire_prod_form()
    if d:
        with db() as c:
            ordre = (c.execute("SELECT COALESCE(MAX(ordre),0)+1 FROM produits").fetchone()[0])
            pid = c.execute("INSERT INTO produits(nom,description,prix,cat,actif,ordre) VALUES(?,?,?,?,1,?)", (*d, ordre)).lastrowid
        enregistrer(f"produit_{pid}")
    return redirect(url_for("admin") + "#produits")

@app.post(f"/{ADMIN_PATH}/produit/<int:pid>")
@admin_requis
def admin_produit(pid):
    action = request.form.get("action", "enregistrer")
    if action == "supprimer":
        with db() as c: c.execute("DELETE FROM produits WHERE id=?", (pid,))
        for old in glob.glob(os.path.join(UP, f"produit_{pid}.*")): os.remove(old)
    elif action == "reset_image":
        for old in glob.glob(os.path.join(UP, f"produit_{pid}.*")): os.remove(old)
    else:
        d = lire_prod_form()
        if d:
            with db() as c:
                c.execute("UPDATE produits SET nom=?, description=?, prix=?, cat=?, actif=? WHERE id=?",
                          (*d, 1 if request.form.get("actif") else 0, pid))
            enregistrer(f"produit_{pid}")
    return redirect(url_for("admin") + f"#prod{pid}")

@app.post(f"/{ADMIN_PATH}/sortir")
def admin_sortir():
    session.pop("admin", None); return redirect(url_for("accueil"))

ADMIN = """<div class="haut"><h1>Administration</h1></div>
{% if not connecte %}
<form method="post" class="bloc"><h2>Connexion</h2>{% if erreur %}<p style="color:#9b1c10">{{erreur}}</p>{% endif %}
<label for="mdp">Mot de passe</label><input id="mdp" name="mdp" type="password" required>
<p><button class="petit">Se connecter</button></p></form>
{% else %}
<div class="bloc"><h2>Logo</h2><div class="adm"><div class="logo" style="margin:0;background:#0e4634;padding:.5rem;border-radius:12px">{{ logo|safe }}</div>
<form method="post" action="{{url_for('admin_image',nom='logo')}}" enctype="multipart/form-data">
<input type="file" name="image" accept="image/png,image/jpeg,image/webp" required aria-label="Nouveau logo"><button class="petit">Changer le logo</button></form>
{% if logo_perso %}<form method="post" action="{{url_for('admin_image',nom='logo')}}"><input type="hidden" name="action" value="reset"><button class="petit clair">Rétablir</button></form>{% endif %}</div></div>
<div class="bloc" id="produits"><h2>Produits</h2>
{% for pid,(nom,desc,prix,cat,actif) in produits.items() %}
<form class="prod" id="prod{{pid}}" method="post" action="{{url_for('admin_produit',pid=pid)}}" enctype="multipart/form-data">
<div class="adm"><div class="vue">{{ svg(pid)|safe }}</div><span class="nom">{{nom}}{% if not actif %} (masqué){% endif %}</span></div>
<label for="n{{pid}}">Nom</label><input id="n{{pid}}" name="nom" value="{{nom}}" maxlength="60" required>
<label for="d{{pid}}">Description</label><input id="d{{pid}}" name="description" value="{{desc}}" maxlength="120">
<div class="ligne2"><div><label for="p{{pid}}">Prix (KMF)</label><input id="p{{pid}}" name="prix" type="number" min="1" value="{{prix}}" required></div>
<div><label for="c{{pid}}">Catégorie</label><select id="c{{pid}}" name="cat">
{% for v,l in (('pizza','Pizza'),('burger','Burger'),('boisson','Boisson')) %}<option value="{{v}}" {{'selected' if v==cat}}>{{l}}</option>{% endfor %}</select></div></div>
<label class="case"><input type="checkbox" name="actif" {{'checked' if actif}}> Disponible sur le site</label>
<label for="i{{pid}}">Nouvelle image (facultatif)</label><input id="i{{pid}}" type="file" name="image" accept="image/png,image/jpeg,image/webp">
<p class="btns"><button class="petit" name="action" value="enregistrer">Enregistrer</button>
<button class="petit clair" name="action" value="reset_image" formnovalidate>Rétablir l'image</button>
<button class="petit clair" name="action" value="supprimer" formnovalidate onclick="return confirm('Supprimer {{nom}} ?')">Supprimer</button></p></form>
{% endfor %}</div>
<form class="bloc prod" method="post" action="{{url_for('admin_produit_nouveau')}}" enctype="multipart/form-data"><h2>Ajouter un produit</h2>
<label for="nn">Nom</label><input id="nn" name="nom" maxlength="60" required>
<label for="nd">Description</label><input id="nd" name="description" maxlength="120">
<div class="ligne2"><div><label for="np">Prix (KMF)</label><input id="np" name="prix" type="number" min="1" required></div>
<div><label for="nc">Catégorie</label><select id="nc" name="cat"><option value="pizza">Pizza</option><option value="burger">Burger</option><option value="boisson">Boisson</option></select></div></div>
<label for="ni">Image (facultatif)</label><input id="ni" type="file" name="image" accept="image/png,image/jpeg,image/webp">
<p class="btns"><button class="petit">Ajouter au menu</button></p></form>
<form method="post" action="{{url_for('admin_sortir')}}" style="padding:0 1.2rem"><button class="petit">Se déconnecter</button></form>
{% endif %}"""

if __name__ == "__main__":
    print(f"Mode MVola : {MODE}")
    app.run(debug=True)
