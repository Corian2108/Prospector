import os
import time
import random
import gspread
import sys
import re
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager

def load_config(env_path: str | Path = ".env"):

    #Carga .env (si existe) y devuelve un dict con:
    #  - SPREADSHEET_KEY (str) -- obligatorio
    #Termina el programa con código 1 si falta la SPREADSHEET_KEY o el archivo de service account.

    env_path = Path(env_path)
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)

    spreadsheet_key = os.getenv("SPREADSHEET_KEY")
    service_account = os.getenv("SERVICE_ACCOUNT_PATH", "service_account.json")

    if not spreadsheet_key:
        sys.exit("ERROR: SPREADSHEET_KEY no encontrada. Agrégala a .env o exporta la variable de entorno.")

    sa_path = Path(service_account)
    if not sa_path.exists():
        sys.exit(f"ERROR: service account JSON no encontrado en '{service_account}'. "
                 "Pon el archivo ahí o ajusta SERVICE_ACCOUNT_PATH en .env.")
        
    print("Configuración cargada. SPREADSHEET_KEY está presente y service account encontrado en:",
          str(sa_path.resolve()))
    
    return {
        "SPREADSHEET_KEY": spreadsheet_key,
        "SERVICE_ACCOUNT_PATH": str(sa_path.resolve())
    }

def iniciar_navegador(headless=False):
    # Inicia Chrome con Selenium en WSL.
    # Parámetros:
    # headless (bool): True para ejecutar sin GUI, False para mostrar ventana.
    # Retorna:
    # driver (webdriver.Chrome): instancia del navegador
    
    profile_dir = os.path.expanduser("~/prospector/chrome-data")
    os.makedirs(profile_dir, exist_ok=True) # crea la carpeta si no existe
    os.chmod(profile_dir, 0o700) # permisos: solo usuario puede leer/escribir/ejecutar
    
    options = Options()
    
    # Flags básicos
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-sandbox")  # necesario en WSL/root
    options.add_argument("--start-maximized")
    
    # Perfil persistente para guardar cookies y sesión
    profile_path = os.path.expanduser("~/chrome-profile")
    options.add_argument(f"--user-data-dir={profile_path}")
    
    # Headless opcional
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    
    # Inicia el driver de Chrome
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    return driver

def scrape_groups_to_sheet():
    #Extrae grupos desde https://www.facebook.com/groups y añade nuevos a la Sheet.
    # 1) Conectar a Google Sheets
    gc = gspread.service_account(filename=service_account_json)
    sh = gc.open_by_key(spreadsheet_key)
    ws = sh.sheet1

    # 2) Leer URLs ya existentes para dedupe (columna GroupURL asumida en header)
    try:
        existing = {row.get("GroupURL").strip() for row in ws.get_all_records() if row.get("GroupURL")}
    except Exception:
        # si la hoja está vacía o sin headers, leer la columna 2 (GroupURL)
        col = ws.col_values(2)
        existing = set(col[1:]) if len(col) > 1 else set()

    # 3) Navegar a la vista de grupos
    driver.get("https://www.facebook.com/groups/joins/?nav_source=tab")
    # esperar que cargue contenido de la página de grupos
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(2)  # pequeña espera extra para que aparezcan los enlaces

    # 4) Encontrar enlaces que parecen corresponder a grupos
    anchors = driver.find_elements(By.TAG_NAME, "a")
    found = {}  # url -> name

    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        text = (a.text or "").strip().split("\n")[0]
        base = href.split("?")[0].rstrip("/")
        
        # heurística: href contiene '/groups/' y el texto no está vacío y no es texto de UI (Join, Ver, etc.)
        if "/groups/" in href and text and len(text) > 3:
            # limpiar parámetros (mantener base hasta el fragment /groups/<id_or_name>)
            # y evitar enlaces a posts dentro de groups (permalinks)
            # preferimos URLs que no contengan 'permalink' o 'posts'
            if ("permalink" in href or "/posts/" in href 
                or "/feed/" in href or "/discover/" in href 
                or "/joins/" in href or "/user/" in href):
                continue
            if base == "https://www.facebook.com/groups":
                continue
            if base not in found:
                found[base] = text

    # 5) Preparar filas nuevas y añadir a Sheet
    new_rows = []
    for url, name in found.items():
        if url in existing:
            continue
        # Row order: GroupName, GroupURL, LastChecked, Active
        new_rows.append([name, url, "", "TRUE"])
        existing.add(url)  # evitar duplicados en la misma run

    if new_rows:
        # Append rows in batch (una por una con append_row)
        for row in new_rows:
            ws.append_row(row, value_input_option="USER_ENTERED")
    # retornar resumen
    return {"found_total": len(found), "added": len(new_rows)}

def scrape_posts():
    
    # Itera hasta 5 grupos (los con LastChecked más antiguo, Active=true),
    # procesa hasta 10 posts por grupo y guarda leads (si tienen teléfono) en
    # la pestaña "Prospectos" del spreadsheet indicado en .env.
    
    # gc = service_account_json
    # sh = gc.open_by_key(spreadsheet_key)

    gc = gspread.service_account(filename=service_account_json)
    sh = gc.open_by_key(spreadsheet_key)

    # hojas: Grupos y Prospectos (crearlas si no existen)
    try:
        ws_groups = sh.worksheet("Groups")
    except gspread.WorksheetNotFound:
        raise RuntimeError("La hoja 'Groups' no existe. Crea la pestaña con headers: GroupName,GroupURL,LastChecked,Active")

    try:
        ws_pros = sh.worksheet("Prospects")
    except gspread.WorksheetNotFound:
        # crear con headers mínimos
        ws_pros = sh.add_worksheet(title="Prospects", rows="1000", cols="20")
        headers = ["Business","Name","Phone","SecondaryPhone","us","Group","Timestamp","NextContact","City","URL","Email", "Channel"]
        ws_pros.append_row(headers, value_input_option="USER_ENTERED")

    # Leer grupos activos y ordenar por LastChecked (vacío primero)
    groups_records = ws_groups.get_all_records()
    active_groups = []
    for r in groups_records:
        if str(r.get("Active", "")).strip().upper() in ("TRUE", "SI", "YES", "1"):
            lc = r.get("LastChecked") or ""
            active_groups.append({"name": r.get("GroupName", ""), "url": r.get("GroupURL", ""), "lastchecked": lc})

    # ordenar: vacíos primero, luego por fecha asc (ISO expected)
    def key_lc(g):
        return (0, "") if not g["lastchecked"] else (1, g["lastchecked"])
    active_groups.sort(key=key_lc)

    # tomar hasta 5 grupos
    groups_to_process = active_groups[:5]

    # preparar dedupe en memoria y leer columna de teléfonos existentes (para evitar lecturas repetidas)
    seen_phones = set()
    try:
        headers = ws_pros.row_values(1)
        tel_col_index = headers.index("Phone") + 1
    except ValueError:
        # si no existe, asumimos columna 3
        tel_col_index = 3

    # leer columna de teléfonos ya guardados en sheet
    try:
        existing_tels = set(ws_pros.col_values(tel_col_index)[1:])  # quitar header
    except Exception:
        existing_tels = set()

    # contadores y límites
    processed_count = 0
    limit_total = 5
    posts_per_group = 5
    delay_between_posts_min = 10
    delay_between_posts_max = 20
    delay_between_groups_min = 30
    delay_between_groups_max = 60

    # helper: scroll para cargar posts dentro de un grupo
    def scroll_and_load_group(driver, max_scrolls=12, pause=1.0):
        body = driver.find_element(By.TAG_NAME, "body")
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(max_scrolls):
            body.send_keys(Keys.END)
            time.sleep(pause)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

    # helper: actualizar LastChecked para la fila del grupo
    def update_lastchecked_in_sheet(url):
        try:
            cell = ws_groups.find(url)
            if cell:
                row = cell.row
                # encontrar índice de columna LastChecked (header)
                headers = ws_groups.row_values(1)
                try:
                    lc_idx = headers.index("LastChecked") + 1
                except ValueError:
                    lc_idx = 3  # asume posición por defecto
                ws_groups.update_cell(row, lc_idx, datetime.now(datetime.UTC).date().isoformat())
        except Exception as e:
            print("Warning: no se pudo actualizar LastChecked para", url, ":", e)

    # main loop por grupos
    for group in groups_to_process:
        if processed_count >= limit_total:
            break

        group_url = group["url"]
        print(f"[SCRAPER] Procesando grupo: {group['name']} -> {group_url}")

        try:
            driver.get(group_url)
            # esperar que cuerpo cargue
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(2)
            scroll_and_load_group(driver, max_scrolls=10, pause=1.2)
        except Exception as e:
            print("Error cargando grupo:", group_url, e)
            update_lastchecked_in_sheet(group_url)
            continue

        # encontrar posts (heurística: role="article")
        try:
            posts = driver.find_elements(By.CSS_SELECTOR, 'div[role="article"]')
        except Exception:
            posts = []

        posts_processed_in_group = 0
        for post in posts:
            if posts_processed_in_group >= posts_per_group or processed_count >= limit_total:
                break

            try:
                time.sleep(random.uniform(delay_between_posts_min, delay_between_posts_max))

                # obtener texto y html
                post_text = post.text or ""
                post_html = post.get_attribute("outerHTML") or ""

                # intentar extraer permalink del post
                post_url = ""
                try:
                    anchors = post.find_elements(By.TAG_NAME, "a")
                    for a in anchors:
                        href = a.get_attribute("href") or ""
                        if "/posts/" in href or "permalink" in href or "/story.php" in href:
                            post_url = href.split("?")[0].rstrip("/")
                            break
                    # fallback: si no encontramos permalink, intentar enlace al post dentro del tiempo (esto puede fallar)
                    if not post_url:
                        # intenta encontrar un botón de tiempo/fecha con href al post
                        time_link = post.find_element(By.CSS_SELECTOR, "a[href*='/permalink/'], a[href*='/posts/'], a[href*='/story.php']")
                        post_url = time_link.get_attribute("href").split("?")[0].rstrip("/")
                except Exception:
                    # si no hay permalink detectable, crear un identificador a partir de la URL actual + un hash (evitar guardar sin link)
                    post_url = driver.current_url

                # extraer autor (intento)
                author = ""
                try:
                    # heurística: buscar elemento que contenga el nombre del autor dentro del article
                    author_el = post.find_element(By.XPATH, ".//h2//span")  # puede fallar según layout
                    author = author_el.text.strip()
                except Exception:
                    # fallback más permisivo
                    try:
                        author_link = post.find_element(By.CSS_SELECTOR, "a[href*='/profile.php'], a[role='link']")
                        author = author_link.text.strip()
                    except Exception:
                        author = ""

                # Extraer teléfonos (usa tu función existente)
                phones = extract_phones(post_text, post_html)
                if not phones:
                    # no phone => skip
                    print("Skip: sin teléfono en post:", post_url)
                    # posts_processed_in_group += 1
                    continue

                # normalizar primario/secundario
                primary = normalize_phone(phones[0])
                secondary = normalize_phone(phones[1]) if len(phones) > 1 else ""

                # validación mínima teléfono
                if not primary or len(primary) < 8:
                    print("Skip: teléfono inválido:", phones, "en post", post_url)
                    # posts_processed_in_group += 1
                    continue

                # dedupe memoria + sheet
                if primary in seen_phones or primary in existing_tels:
                    print("Skip duplicado:", primary)
                    # posts_processed_in_group += 1
                    continue

                # preparar fila para guardar en Prospectos
                # Asegurarse de que headers existan y orden:
                headers = ws_pros.row_values(1)
                # generar mapping header->index
                header_map = {h: i+1 for i, h in enumerate(headers)}

                # si falta alguna columna básica, crear una fila headers estándar (defensivo)
                required_headers = ["Business","Name","Phone","SecondaryPhone","Status","Group","Timestamp","NextContact","City","URL","Email", "Channel"]
                if not all(h in header_map for h in required_headers):
                    ws_pros.clear()
                    ws_pros.append_row(required_headers, value_input_option="USER_ENTERED")
                    header_map = {h: i+1 for i, h in enumerate(required_headers)}

                today_iso = datetime.now(datetime.UTC).date().isoformat()
                row = [""] * len(header_map)
                # asigna valores según header_map
                row[header_map["Business"]-1] = ""           # opcional, difícil de extraer reliably
                row[header_map["Name"]-1] = author
                row[header_map["Phone"]-1] = primary
                row[header_map["SecondaryPhone"]-1] = secondary
                row[header_map["Status"]-1] = "Nuevo"
                row[header_map["Group"]-1] = group['name']
                row[header_map["Timestamp"]-1] = today_iso
                row[header_map["NextContact"]-1] = ""
                row[header_map["City"]-1] = ""            # extraer ciudad es opcional/heurístico
                row[header_map["URL"]-1] = post_url
                row[header_map["Email"]-1] = ""             # intentar extraer con regex si quieres
                row[header_map["Channel"]-1] = "FB"

                # append a sheet
                ws_pros.append_row(row, value_input_option="USER_ENTERED")

                # marcar dedupe
                seen_phones.add(primary)
                existing_tels.add(primary)
                processed_count += 1
                posts_processed_in_group += 1

                print(f"[GUARDADO] {primary}  ({author}) -> {post_url}  (total guardados: {processed_count})")

            except Exception as e:
                print("Error procesando post:", e)
                #Si hay error al procesar el post, no se cuenta
                # posts_processed_in_group += 1
                continue

        # actualización LastChecked del grupo procesado
        update_lastchecked_in_sheet(group_url)

        # delay entre grupos
        if processed_count < limit_total:
            sleep_t = random.uniform(delay_between_groups_min, delay_between_groups_max)
            print(f"Delay {sleep_t:.1f}s antes del siguiente grupo...")
            time.sleep(sleep_t)

    # fin de run: resumen
    print("=== RUN COMPLETADO ===")
    print("Guardados totales:", processed_count)
    print("Terminado a:", datetime.now(datetime.UTC).isoformat())

def extract_phones(post_text: str, post_html: str) -> list[str]:
    # Busca números de teléfono en el texto o html de la publicación.
    # Puede capturar formatos:
    # - +593xxxxxxxxx
    # - 09xxxxxxxx
    # - wa.me/593xxxxxxxxx
    # - whatsapp.com/send?phone=593xxxxxxxxx
    # Retorna lista de números crudos (sin normalizar).
    
    phones = set()

    # --- Buscar en texto ---
    if post_text:
        # +593xxxxxxxxx
        matches = re.findall(r"\+593\d{9}", post_text)
        phones.update(matches)

        # 09xxxxxxxx
        matches = re.findall(r"\b09\d{8}\b", post_text)
        phones.update(matches)

    # --- Buscar en html (links wa.me o api whatsapp) ---
    if post_html:
        # wa.me/593xxxxxxxxx
        matches = re.findall(r"wa\.me/(\d{9,12})", post_html)
        phones.update(matches)

        # whatsapp.com/send?phone=593xxxxxxxxx
        matches = re.findall(r"phone=(\d{9,12})", post_html)
        phones.update(matches)

    return list(phones)

def normalize_phone(raw_phone: str) -> str:
    
    # Normaliza un número de teléfono ecuatoriano a formato +593xxxxxxxxx.
    if not raw_phone:
        return None

    # limpiar espacios, guiones y símbolos
    phone = re.sub(r"[^\d+]", "", raw_phone)

    # ya está en formato 593
    if phone.startswith("593") and len(phone) == 12:
        return phone

    # formato 09xxxxxxxx (celular)
    if phone.startswith("09") and len(phone) == 10:
        return "593" + phone[1:]

    # formato 0Xxxxxxxx (teléfonos fijos, opcionales)
    if phone.startswith("0") and len(phone) in [8,9]:
        return "593" + phone[1:]

    return phone

# Ejecución main:
if __name__ == "__main__":
    driver = iniciar_navegador(headless=False)
    driver.get("https://www.facebook.com")
    cfg = load_config()
    spreadsheet_key = cfg["SPREADSHEET_KEY"]
    service_account_json = cfg["SERVICE_ACCOUNT_PATH"]
    # scrape_groups_to_sheet()
    scrape_posts()
    input("Presiona Enter para cerrar el navegador...")
    driver.quit()
