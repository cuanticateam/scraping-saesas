# -*- coding: utf-8 -*-
"""
Modulo para sincronizar datos de SAESAS con Google Sheets.
Columnas: TIPO, DIRECCION, AREA, FMI, PRECIO, LINK
"""

import json, os
from datetime import datetime, timedelta, timezone

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ══ CONFIGURAR ESTE ID despues de crear el Google Sheet ══
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID_SAESAS", "1JCpVLGTQrRkxwmWVCAx6ohauH92cDUTmSdnkWhUjhZs")

CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scraping-activos-3539aac681f8.json")

COLOR_HEADER  = {"red": 0.43, "green": 0.10, "blue": 0.47}   # 6E1978
COLOR_HDR2    = {"red": 0.55, "green": 0.15, "blue": 0.60}   # 8C2699
COLOR_BLANCO  = {"red": 1.0,  "green": 1.0,  "blue": 1.0}
COLOR_FILA    = {"red": 0.96, "green": 0.93, "blue": 0.97}   # F5EEF8
COLOR_FILA2   = {"red": 1.0,  "green": 1.0,  "blue": 1.0}    # blanco
COLOR_CAMBIO  = {"red": 1.0,  "green": 0.85, "blue": 0.85}   # rojo claro
COLOR_NUEVO   = {"red": 0.85, "green": 1.0,  "blue": 0.85}   # verde claro

COLS = ["NOMBRE", "TIPO", "DIRECCION", "AREA", "FMI", "PRECIO", "LINK"]
CAMPOS = ["nombre", "tipo", "direccion", "area", "fmi", "precio", "link"]
ANCHOS = [250, 180, 320, 120, 180, 150, 450]

COL_TZ = timezone(timedelta(hours=-5))


def get_client():
    import gspread
    from google.oauth2.service_account import Credentials

    if os.path.exists(CREDS_FILE):
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        return gspread.authorize(creds)

    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)

    raise Exception("No se encontraron credenciales de Google Sheets")


def sync_to_sheets(inmuebles, cambios):
    print("  Conectando con Google Sheets...")

    if not SPREADSHEET_ID:
        print("  ADVERTENCIA: SPREADSHEET_ID_SAESAS no configurado. Saltando sync.")
        return

    client = get_client()
    sh = client.open_by_key(SPREADSHEET_ID)

    hojas_existentes = {w.title: w for w in sh.worksheets()}

    ws = _obtener_hoja(sh, hojas_existentes, "Arriendos Medellin", len(inmuebles))
    ws_info = _obtener_hoja(sh, hojas_existentes, "Info", 5)

    if "Hoja 1" in hojas_existentes:
        try:
            sh.del_worksheet(hojas_existentes["Hoja 1"])
        except Exception:
            pass

    print("  Escribiendo datos...")
    _escribir_datos(ws, inmuebles, cambios)

    print("  Escribiendo info...")
    ws_info.clear()
    _escribir_info(ws_info, len(inmuebles))

    print(f"  Google Sheets actualizado: {sh.url}")


def _obtener_hoja(sh, existentes, nombre, filas_min):
    if nombre in existentes:
        return existentes[nombre]
    return sh.add_worksheet(title=nombre, rows=max(filas_min + 5, 10), cols=len(COLS))


def _escribir_datos(ws, inmuebles, cambios):
    # Construir filas
    filas = []
    filas.append(["INMUEBLES EN ARRIENDO - SAESAS - MEDELLIN"] + [""] * (len(COLS) - 1))
    filas.append(COLS)

    for item in inmuebles:
        fila = [str(item.get(campo, "")) for campo in CAMPOS]
        filas.append(fila)

    ws.clear()
    if filas:
        ws.update(filas, value_input_option="RAW")

    total_filas = len(filas)
    try:
        if ws.row_count < total_filas:
            ws.resize(rows=total_filas + 2, cols=len(COLS))
    except Exception:
        pass

    # Formato
    requests = []
    ws_id = ws.id

    # Merge titulo
    requests.append({
        "mergeCells": {
            "range": _rango(ws_id, 0, 0, 1, len(COLS)),
            "mergeType": "MERGE_ALL"
        }
    })

    # Formato titulo
    requests.append(_formato_celdas(ws_id, 0, 0, 1, len(COLS),
                                     COLOR_HEADER, COLOR_BLANCO, True, 14))

    # Formato encabezados
    requests.append(_formato_celdas(ws_id, 1, 0, 2, len(COLS),
                                     COLOR_HDR2, COLOR_BLANCO, True, 10))

    # Formato filas de datos
    for i, item in enumerate(inmuebles):
        fila_idx = i + 2
        pid = item.get("_id", "")
        bg = COLOR_FILA if i % 2 == 0 else COLOR_FILA2

        # Verificar si este inmueble tiene cambios
        tiene_cambio = any(
            f"{pid}:{campo}" in cambios for campo in CAMPOS
        )

        for j, campo in enumerate(CAMPOS):
            clave = f"{pid}:{campo}"
            if clave in cambios:
                cell_bg = COLOR_CAMBIO
            elif tiene_cambio and not any(f"{pid}:" in k for k in cambios):
                cell_bg = COLOR_NUEVO
            else:
                cell_bg = bg
            requests.append(_formato_celdas(ws_id, fila_idx, j, fila_idx + 1, j + 1,
                                             cell_bg, None, False, 10))

    # Wrap text en columna LINK
    idx_link = CAMPOS.index("link")
    requests.append({
        "repeatCell": {
            "range": _rango(ws_id, 2, idx_link, total_filas, idx_link + 1),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy"
        }
    })

    # Anchos de columna
    for j, ancho in enumerate(ANCHOS):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                          "startIndex": j, "endIndex": j + 1},
                "properties": {"pixelSize": ancho},
                "fields": "pixelSize"
            }
        })

    # Congelar filas
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": ws_id,
                "gridProperties": {"frozenRowCount": 2}
            },
            "fields": "gridProperties.frozenRowCount"
        }
    })

    # Filtros
    requests.append({"clearBasicFilter": {"sheetId": ws_id}})
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": _rango(ws_id, 1, 0, total_filas, len(COLS))
            }
        }
    })

    # Ejecutar
    if requests:
        for chunk_start in range(0, len(requests), 100):
            chunk = requests[chunk_start:chunk_start + 100]
            ws.spreadsheet.batch_update({"requests": chunk})


def _escribir_info(ws, n_inmuebles):
    ahora = datetime.now(COL_TZ).strftime("%Y-%m-%d %H:%M")
    filas = [
        ["Ultima actualizacion", ahora],
        ["Inmuebles en arriendo", f"{n_inmuebles}"],
        ["Ciudad", "Medellin"],
        ["Fuente", "saesas.gov.co"],
    ]
    ws.update(filas)
    ws_id = ws.id
    reqs = [
        _formato_celdas(ws_id, 0, 0, 4, 1, None, None, True, 10),
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 200}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                      "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 250}, "fields": "pixelSize"
        }}
    ]
    ws.spreadsheet.batch_update({"requests": reqs})


# ── Helpers de formato ──

def _rango(sheet_id, r1, c1, r2, c2):
    return {"sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def _formato_celdas(sheet_id, r1, c1, r2, c2, bg=None, fg=None, bold=False, size=10):
    fmt = {"fontSize": size}
    if bold:
        fmt["bold"] = True
    if fg:
        fmt["foregroundColorStyle"] = {"rgbColor": fg}
    cell_fmt = {"textFormat": fmt}
    if bg:
        cell_fmt["backgroundColor"] = bg
    return {
        "repeatCell": {
            "range": _rango(sheet_id, r1, c1, r2, c2),
            "cell": {"userEnteredFormat": cell_fmt},
            "fields": "userEnteredFormat"
        }
    }
