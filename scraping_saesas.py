# -*- coding: utf-8 -*-
"""
Scraping SAESAS - saesas.gov.co
Extrae inmuebles en arriendo en Medellin, detecta cambios y notifica por email.
Funciona local y en GitHub Actions.
"""

import requests, json, re, os, smtplib, time, urllib3
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

# Suprimir warnings de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACION
# ═══════════════════════════════════════════════════════════════════════════════

COL_TZ = timezone(timedelta(hours=-5))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATOS_FILE   = os.path.join(SCRIPT_DIR, "datos_anteriores_saesas.json")
CAMBIOS_FILE = os.path.join(SCRIPT_DIR, "registro_cambios_saesas.json")

BASE_URL = "https://www.saesas.gov.co"
LISTING_URL = (
    BASE_URL
    + "/transparencia-y-acceso-a-la-informacion-publica"
    "/5-tramites-y-servicios/arriendos/arrendamientos"
)
REGION_MEDELLIN = "05001"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
}

# Email
EMAIL_REMITENTE    = os.environ.get("EMAIL_REMITENTE", "cuanticateamsas@gmail.com")
EMAIL_CONTRASENA   = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_DESTINATARIO = os.environ.get("EMAIL_DESTINATARIO", "cuanticateamsas@gmail.com")

DIAS_ROJO = 2


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SCRAPING - LISTADO
# ═══════════════════════════════════════════════════════════════════════════════

def obtener_links_listado():
    """Recorre todas las paginas del listado y extrae links a detalles."""
    links = []
    pagina = 1

    while True:
        params = {"region": REGION_MEDELLIN, "page": pagina}
        print(f"  Pagina {pagina}...")

        for intento in range(3):
            try:
                resp = requests.get(
                    LISTING_URL, params=params, headers=HEADERS,
                    verify=False, timeout=30
                )
                resp.raise_for_status()
                break
            except Exception:
                if intento < 2:
                    time.sleep(3)
                else:
                    raise

        soup = BeautifulSoup(resp.text, "html.parser")

        # Buscar links a detalle de inmuebles
        encontrados = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/arriendos/arrendamientos/" in href and href != LISTING_URL:
                # Excluir el link del breadcrumb (que apunta solo a /arrendamientos)
                slug = href.rstrip("/").split("/")[-1]
                if slug and slug != "arrendamientos":
                    full_url = href if href.startswith("http") else BASE_URL + href
                    if full_url not in links:
                        links.append(full_url)
                        encontrados += 1

        if encontrados == 0:
            break

        # Verificar si hay pagina siguiente
        next_link = soup.find("a", string=re.compile(r"Next"))
        if not next_link:
            break

        pagina += 1

    return links


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SCRAPING - DETALLE DE CADA INMUEBLE
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_detalle(url, idx, total):
    """Extrae datos de la pagina de detalle de un inmueble."""
    for intento in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, verify=False, timeout=30)
            resp.raise_for_status()
            break
        except Exception:
            if intento < 2:
                time.sleep(3)
            else:
                print(f"    ERROR: no se pudo cargar {url}")
                return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extraer campos del bloque de detalles
    def buscar_campo(label):
        tag = soup.find("strong", string=re.compile(label, re.IGNORECASE))
        if tag and tag.parent:
            texto = tag.parent.get_text(separator=" ", strip=True)
            # Quitar el label
            valor = re.sub(re.escape(tag.get_text()), "", texto, count=1).strip()
            # Limpiar ":" al inicio
            valor = valor.lstrip(":").strip()
            return valor
        return ""

    tipo = buscar_campo(r"Tipo inmueble")
    fmi = buscar_campo(r"FMI")
    direccion = buscar_campo(r"Direcci[oó]n")
    area_terreno = buscar_campo(r"[AÁ]rea terreno")
    area_construida = buscar_campo(r"[AÁ]rea construida")
    municipio = buscar_campo(r"Municipio")

    # Usar area construida si > 0, sino area terreno
    area = area_construida or area_terreno
    # Limpiar "0 mts²" -> preferir la otra
    if area and re.match(r"^0\s", area):
        area = area_terreno if area == area_construida else area_construida
    if area and re.match(r"^0\s", area):
        area = "0 mts²"

    # Precio
    precio = ""
    precio_tag = soup.find("p", class_=re.compile(r"text-.*pink|text-.*bold"))
    if precio_tag:
        precio = precio_tag.get_text(strip=True)
    if not precio:
        p_label = soup.find("p", string=re.compile(r"Precio de arriendo", re.IGNORECASE))
        if p_label and p_label.find_next_sibling("p"):
            precio = p_label.find_next_sibling("p").get_text(strip=True)

    # Titulo (h1)
    titulo = ""
    h1 = soup.find("h1")
    if h1:
        titulo = h1.get_text(strip=True)

    # Si no encontro tipo, extraerlo del titulo
    if not tipo and titulo:
        tipo = titulo.split("-")[0].strip() if "-" in titulo else titulo

    slug = url.rstrip("/").split("/")[-1]
    print(f"  [{idx}/{total}] {titulo or slug}")

    return {
        "_id": slug,
        "tipo": tipo,
        "direccion": direccion,
        "area": area,
        "fmi": fmi,
        "precio": precio,
        "link": url,
        "titulo": titulo,
        "municipio": municipio,
    }


def scrape_todos(links):
    """Scrape detalle de todos los inmuebles."""
    inmuebles = []
    total = len(links)
    for i, url in enumerate(links, 1):
        datos = scrape_detalle(url, i, total)
        if datos:
            inmuebles.append(datos)
        time.sleep(0.5)  # cortesia
    return inmuebles


# ═══════════════════════════════════════════════════════════════════════════════
# 3. DETECCION DE CAMBIOS
# ═══════════════════════════════════════════════════════════════════════════════

CAMPOS_COMPARAR = ["tipo", "direccion", "area", "fmi", "precio"]


def cargar_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detectar_cambios(inmuebles):
    anteriores = cargar_json(DATOS_FILE)
    registro = cargar_json(CAMBIOS_FILE)
    ahora = datetime.now(COL_TZ).isoformat()
    limite = (datetime.now(COL_TZ) - timedelta(days=DIAS_ROJO)).isoformat()
    resumen = []
    cambios_ahora = {}

    ids_actuales = set()

    for item in inmuebles:
        pid = item["_id"]
        ids_actuales.add(pid)
        prev = anteriores.get(pid, {})

        # Inmueble NUEVO
        if not prev:
            resumen.append({
                "tipo_cambio": "NUEVO",
                "titulo": item.get("titulo", "?"),
                "tipo": item.get("tipo", ""),
                "precio": item.get("precio", ""),
                "link": item.get("link", ""),
            })
            for campo in CAMPOS_COMPARAR:
                registro[f"{pid}:{campo}"] = ahora
                cambios_ahora[f"{pid}:{campo}"] = True

        # Cambios en campos
        for campo in CAMPOS_COMPARAR:
            nuevo = str(item.get(campo, ""))
            viejo = str(prev.get(campo, ""))
            if prev and nuevo != viejo:
                registro[f"{pid}:{campo}"] = ahora
                cambios_ahora[f"{pid}:{campo}"] = True
                resumen.append({
                    "tipo_cambio": "CAMBIO",
                    "titulo": item.get("titulo", "?"),
                    "campo": campo, "antes": viejo, "ahora": nuevo,
                })

        anteriores[pid] = {c: str(item.get(c, "")) for c in CAMPOS_COMPARAR}
        anteriores[pid]["titulo"] = item.get("titulo", "")

    # Inmuebles ELIMINADOS
    for clave in list(anteriores.keys()):
        if clave not in ids_actuales:
            resumen.append({
                "tipo_cambio": "ELIMINADO",
                "titulo": anteriores[clave].get("titulo", "?"),
                "tipo": anteriores[clave].get("tipo", ""),
            })
            del anteriores[clave]

    # Limpiar cambios viejos
    for k in list(registro.keys()):
        if registro[k] < limite:
            del registro[k]

    guardar_json(DATOS_FILE, anteriores)
    guardar_json(CAMBIOS_FILE, registro)
    return cambios_ahora, resumen


# ═══════════════════════════════════════════════════════════════════════════════
# 4. EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

def enviar_email(resumen):
    if not EMAIL_CONTRASENA or not resumen:
        print("  Email desactivado (sin contrasena) o sin cambios")
        return

    try:
        nuevos = [c for c in resumen if c["tipo_cambio"] == "NUEVO"]
        eliminados = [c for c in resumen if c["tipo_cambio"] == "ELIMINADO"]
        cambios = [c for c in resumen if c["tipo_cambio"] == "CAMBIO"]

        fecha = datetime.now(COL_TZ).strftime("%d/%m/%Y %H:%M")

        partes = []
        if nuevos: partes.append(f"{len(nuevos)} nuevos")
        if eliminados: partes.append(f"{len(eliminados)} eliminados")
        if cambios: partes.append(f"{len(cambios)} cambios")
        asunto = f"SAESAS Arriendos Medellin - {', '.join(partes)}"

        ESTILO_TABLA = (
            "border-collapse:collapse;width:100%;font-family:Arial,sans-serif;"
            "font-size:13px;margin-bottom:20px;"
        )
        ESTILO_TH = (
            "background-color:#6E1978;color:white;padding:8px 12px;"
            "text-align:left;border:1px solid #ccc;"
        )
        ESTILO_TD = "padding:8px 12px;border:1px solid #ddd;"
        ESTILO_TD_ALT = ESTILO_TD + "background-color:#f8f8f8;"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;">
        <h2 style="color:#6E1978;margin-bottom:5px;">Alerta - SAESAS Arriendos Medellin</h2>
        <p style="color:#666;margin-top:0;">{fecha}</p>
        """

        # NUEVOS
        if nuevos:
            html += f'<h3 style="color:#2E7D32;">Nuevos inmuebles ({len(nuevos)})</h3>'
            html += f'<table style="{ESTILO_TABLA}">'
            html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
            html += f'<th style="{ESTILO_TH}">Tipo</th>'
            html += f'<th style="{ESTILO_TH}">Precio</th>'
            html += f'<th style="{ESTILO_TH}">Link</th></tr>'
            for i, c in enumerate(nuevos):
                td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                link_html = f'<a href="{c.get("link","")}" style="color:#6E1978;">Ver</a>'
                html += f'<tr><td style="{td}">{c["titulo"]}</td>'
                html += f'<td style="{td}">{c.get("tipo","")}</td>'
                html += f'<td style="{td}">{c.get("precio","")}</td>'
                html += f'<td style="{td}">{link_html}</td></tr>'
            html += '</table>'

        # CAMBIOS
        if cambios:
            html += f'<h3 style="color:#1565C0;">Cambios detectados ({len(cambios)})</h3>'
            html += f'<table style="{ESTILO_TABLA}">'
            html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
            html += f'<th style="{ESTILO_TH}">Campo</th>'
            html += f'<th style="{ESTILO_TH}">Antes</th>'
            html += f'<th style="{ESTILO_TH}">Ahora</th></tr>'
            for i, c in enumerate(cambios):
                td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                html += f'<tr><td style="{td}">{c["titulo"]}</td>'
                html += f'<td style="{td}">{c["campo"]}</td>'
                html += f'<td style="{td}">{c.get("antes","") or "-"}</td>'
                html += f'<td style="{td}">{c.get("ahora","") or "-"}</td></tr>'
            html += '</table>'

        # ELIMINADOS
        if eliminados:
            html += f'<h3 style="color:#C62828;">Inmuebles eliminados ({len(eliminados)})</h3>'
            html += f'<table style="{ESTILO_TABLA}">'
            html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
            html += f'<th style="{ESTILO_TH}">Tipo</th></tr>'
            for i, c in enumerate(eliminados):
                td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                html += f'<tr><td style="{td}">{c["titulo"]}</td>'
                html += f'<td style="{td}">{c.get("tipo","")}</td></tr>'
            html += '</table>'

        html += """
        <p style="color:#999;font-size:12px;margin-top:20px;">
        Tabla actualizada en Google Sheets<br>
        Fuente: saesas.gov.co
        </p></div>
        """

        msg = MIMEMultipart()
        msg["From"] = EMAIL_REMITENTE
        msg["To"] = EMAIL_DESTINATARIO
        msg["Subject"] = asunto
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.send_message(msg)
        print(f"  Email enviado a {EMAIL_DESTINATARIO}")
    except Exception as e:
        print(f"  Error enviando email: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  Scraping SAESAS - saesas.gov.co")
    print("  Inmuebles en arriendo - Medellin")
    print("=" * 55)

    # Obtener links del listado
    print("\n[1/4] Obteniendo listado de inmuebles...")
    links = obtener_links_listado()
    print(f"  {len(links)} inmuebles encontrados")

    # Scrape detalle
    print("\n[2/4] Extrayendo detalles de cada inmueble...")
    inmuebles = scrape_todos(links)
    print(f"  {len(inmuebles)} inmuebles procesados")

    # Detectar cambios
    print("\n[3/4] Detectando cambios...")
    cambios, resumen = detectar_cambios(inmuebles)

    if resumen:
        print(f"\n  *** {len(resumen)} CAMBIOS DETECTADOS ***")
        for c in resumen[:15]:
            if c["tipo_cambio"] == "CAMBIO":
                print(f"    - {c['titulo']}: {c['campo']} {c.get('antes','')} -> {c.get('ahora','')}")
            else:
                print(f"    - {c['tipo_cambio']} {c['titulo']}")
        if len(resumen) > 15:
            print(f"    ... y {len(resumen)-15} mas")
    else:
        print("  Sin cambios respecto a la ultima actualizacion")

    # Google Sheets
    print("\n[4/4] Actualizando Google Sheets...")
    from sheets_sync_saesas import sync_to_sheets
    sync_to_sheets(inmuebles, cambios)

    # Email
    if resumen:
        enviar_email(resumen)

    print(f"\nListo! {len(inmuebles)} inmuebles en arriendo en Medellin")
