"""Build a non-transactional, allowlisted BYSG preview for Vercel Hobby."""

from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist"
PAGES = (
    "index.html",
    "nosotros.html",
    "opiniones.html",
    "talles.html",
    "preguntas-frecuentes.html",
    "envios.html",
    "cambios-y-devoluciones.html",
    "privacidad.html",
    "terminos.html",
    "contacto.html",
    "seguimiento.html",
    "arrepentimiento.html",
)
ASSETS = ("assets/branding/", "assets/productos/", "assets/talles/")
BANNER = '<div class="preview-banner" role="status">VISTA PRIVADA DE PRUEBA · No se aceptan pedidos ni pagos</div>'
HEAD = '<meta name="robots" content="noindex,nofollow,noarchive"><link rel="stylesheet" href="preview.css"><script src="preview.js" defer></script>'
FORM_NOTICE = '<p class="preview-form-notice">Esta función no está disponible en la vista de prueba.</p>'


def tracked_assets():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *ASSETS],
        cwd=ROOT, check=True, capture_output=True,
    )
    for raw_path in result.stdout.split(b"\0"):
        if raw_path:
            path = Path(raw_path.decode("utf-8"))
            if not (ROOT / path).is_file():
                raise FileNotFoundError(path)
            yield path


def build():
    if OUTPUT.parent != ROOT or OUTPUT.name != "dist":
        raise RuntimeError("Unsafe output directory")
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir()

    for path in tracked_assets():
        target = OUTPUT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)

    shutil.copy2(ROOT / "sistema.css", OUTPUT / "sistema.css")
    shutil.copy2(ROOT / "deploy/preview.css", OUTPUT / "preview.css")
    shutil.copy2(ROOT / "deploy/preview.js", OUTPUT / "preview.js")

    for page in PAGES:
        html = (ROOT / page).read_text(encoding="utf-8")
        if page != "index.html":
            html = re.sub(r"<form\b[^>]*>.*?</form>", FORM_NOTICE, html, flags=re.I | re.S)
            html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
            html = re.sub(r"<script\b[^>]*/>", "", html, flags=re.I | re.S)
        else:
            html = html.replace('<script src="store-api.js"></script>', "")
        if "</head>" not in html or "<body>" not in html:
            raise RuntimeError(f"Unexpected page structure: {page}")
        html = html.replace("</head>", HEAD + "</head>", 1)
        html = html.replace("<body>", "<body>" + BANNER, 1)
        (OUTPUT / page).write_text(html, encoding="utf-8")

    forbidden = ("checkout.html", "admin.html", "bauti.html", "server.py", "data", "backups", ".env")
    if any((OUTPUT / name).exists() for name in forbidden):
        raise RuntimeError("Private or transactional file in preview output")
    print(f"Built private preview with {len(PAGES)} pages; no checkout or backend")


if __name__ == "__main__":
    build()
