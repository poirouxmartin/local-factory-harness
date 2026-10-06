"""The studio must run with no network at all.

A single @import to a font CDN turns a working offline studio into a blank
one on a plane -- and it is the kind of line that gets pasted in without
thinking. This test is the mechanical guarantee, and it is cheap.

The w3.org SVG namespace is an XML identifier, not a fetch: it is allowed.
"""
import re

import factory_web

WEB = factory_web.WEB_DIR
ALLOWED = ("http://www.w3.org/",)
URL = re.compile(r"https?://[^\s\"')]+")


def external_urls(text):
    return [u for u in URL.findall(text) if not u.startswith(ALLOWED)]


def test_no_external_url_in_served_html_css_and_svg():
    paths = WEB.glob("*.html"), WEB.glob("*.css"), WEB.glob("*.svg")
    for path in sorted(p for group in paths for p in group):
        assert external_urls(path.read_text(encoding="utf-8")) == [], path.name


def test_fonts_are_self_hosted():
    css = (WEB / "tokens.css").read_text(encoding="utf-8")
    assert css.count("@font-face") >= 2
    for family in ('"Instrument Sans"', '"JetBrains Mono"'):
        assert family in css
    assert 'url("/static/instrument-sans.woff2")' in css
    assert 'url("/static/jetbrains-mono.woff2")' in css


def test_font_files_exist_and_are_woff2():
    for name in ("instrument-sans.woff2", "jetbrains-mono.woff2"):
        blob = (WEB / name).read_bytes()
        assert blob[:4] == b"wOF2", name
        assert len(blob) > 10_000, name


# --- garde-fous mécaniques ajoutés avec le lot DA du 29/07 ----------------

def test_style_holds_no_literal_colour():
    """tokens.css dit que style.css « ne porte aucune couleur littérale ».
    Ce n'était plus vrai (dix valeurs en dur), et c'est ce qui fait qu'un
    changement de DA repeint certaines vues et pas d'autres."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", css) == []


def test_the_timestamp_is_not_floated_next_to_a_prompt():
    """La régression exacte du 29/07 : une bulle prend une largeur au plus
    juste, donc un `float: right` ne tient jamais à côté d'un texte court et
    retombe à la ligne -- tous les prompts s'affichaient sur deux lignes.
    Le test épingle la cause, pas le rendu : il ne remplace pas une mesure
    dans un vrai navigateur, il empêche de réintroduire la ligne fautive."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\.chat-ts\s*\{[^}]*\}", css)
    assert rule, "la règle .chat-ts a disparu"
    assert "float" not in rule.group(0)
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'class: "chat-row"' in app, "l'horodatage doit être posé dans une " \
        "rangée avec le texte, pas au-dessus de lui"


def test_a_link_is_styled_at_all():
    """Aucune règle ne visait `a` : le navigateur posait son bleu d'usine
    (#0000EE mesuré sur le Dashboard), soit 1,2:1 sur le fond sombre. Les
    identifiants de job des « Derniers échecs » étaient illisibles."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"(?m)^a\s*\{[^}]*\}", css)
    assert rule, "les liens n'ont plus de règle : retour au bleu du navigateur"
    assert "color" in rule.group(0)


def test_every_pictogram_asked_for_exists_in_the_sprite():
    """`icon("nope")` ne lève rien : le <use> ne résout pas et le bouton reste
    vide. Un bouton dessiné sans dessin est un bouton invisible."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    sprite = (WEB / "icons.svg").read_text(encoding="utf-8")
    have = set(re.findall(r'id="ico-([\w-]+)"', sprite))
    want = set(re.findall(r'\bicon\(\s*"([\w-]+)"', app))
    want |= set(re.findall(r'\biconBtn\(\s*"([\w-]+)"', app))
    want |= set(re.findall(r'\biconLink\(\s*"([\w-]+)"', app))
    want |= set(re.findall(r'\bsoonCard\(\s*"([\w-]+)"', app))
    # Les titres et les mesures dessinés passent par leurs propres fabriques :
    # sans elles ici, un nom de pictogramme faux y resterait invisible.
    want |= set(re.findall(r'\bheading\(\s*"h[1-6]",\s*"([\w-]+)"', app))
    want |= set(re.findall(r'\bstat\(\s*"([\w-]+)"', app))
    want |= set(re.findall(r'\bcard\(\s*"([\w-]+)"', app))
    assert want, "aucun pictogramme trouvé : le motif de recherche a bougé"
    assert want - have == set()


def test_the_scroll_button_carries_a_drawing():
    """Le rond jaune était vide : `h("div", {class:"scroll-btn"})` n'avait
    aucun enfant et aucune règle `content`. Un bouton dessiné sans dessin."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    block = re.search(r'scrollBtn = h\(.*?\n(?:.*?\n){0,6}?.*?append\(icon\('
                      r'"([\w-]+)"\)\)', app, re.S)
    assert block, "le bouton « retour en bas » n'a plus de pictogramme"
    sprite = (WEB / "icons.svg").read_text(encoding="utf-8")
    assert 'id="ico-{}"'.format(block.group(1)) in sprite


def test_the_chat_side_columns_are_emptied_when_leaving_the_view():
    """Les deux colonnes du chat vivent hors de #main : `MAIN.replaceChildren`
    ne les touche pas, et la liste des sessions restait affichée à côté des
    jobs. Le test épingle le nettoyage, pas son rendu."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    leave = re.search(r"if \(wasChat && !isChat\) \{(.*?)\n  \}", app, re.S)
    assert leave, "le bloc « on quitte le chat » a bougé"
    body = leave.group(1)
    assert "chat-sessions-list" in body and "plan-slot-container" in body
    assert "replaceChildren()" in body


def test_the_chat_log_is_pinned_to_the_bottom_after_insertion():
    """Hors du DOM, scrollHeight vaut 0 : le saut initial partait sur un
    élément sans dimensions et n'arrivait jamais en bas. L'ordre est la
    correction — le pin doit suivre l'insertion dans #main."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    insert = app.index("MAIN.replaceChildren(chatContent)")
    pin = app.index("logEl.scrollTop = logEl.scrollHeight")
    assert pin > insert


def test_html_references_the_sprite_symbols_it_uses():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    sprite = (WEB / "icons.svg").read_text(encoding="utf-8")
    have = set(re.findall(r'id="ico-([\w-]+)"', sprite))
    want = set(re.findall(r'icons\.svg#ico-([\w-]+)', html))
    assert want - have == set()


def test_every_asset_carries_the_same_cache_marker():
    """Le commentaire d'index.html le dit : sans marque commune, un studio déjà
    ouvert garde l'ancien app.js et le correctif ne se voit jamais. Une marque
    oubliée sur UN fichier suffit à rejouer ça."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assets = re.findall(r'/static/([\w.-]+\.(?:css|js))(\?v=\d+)?', html)
    assert assets, "index.html ne référence plus aucun asset local"
    markers = {marker for _, marker in assets}
    assert "" not in markers, "un asset sans ?v= : {}".format(
        [name for name, marker in assets if not marker])
    assert len(markers) == 1, "marques divergentes : {}".format(sorted(markers))


def test_woff2_and_svg_have_a_mime_type():
    assert factory_web.MIME[".woff2"] == "font/woff2"
    assert factory_web.MIME[".svg"] == "image/svg+xml"
