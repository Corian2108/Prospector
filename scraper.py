import os
import time
import random
import gspread
import sys
import re
import platform
import pyperclip as clip
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import (
    StaleElementReferenceException,
    ElementClickInterceptedException,
    NoSuchElementException,
)

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
        headers = ["Business","Name","Phone","SecondaryPhone","Status","Group","Timestamp","NextContact","City","URL","Email", "Channel"]
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
    limit_total = 50
    posts_per_group = 10
    delay_between_groups_min = 25
    delay_between_groups_max = 35

    # helper: scroll para cargar posts dentro de un grupo
    def scroll_and_load_group(driver, max_scrolls, pause):
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
                ws_groups.update_cell(row, lc_idx, datetime.now().date().isoformat())
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
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='feed']")))
            time.sleep(2)
            feed = driver.find_element(By.CSS_SELECTOR, "div[role='feed']")
            scroll_and_load_group(driver, max_scrolls=2, pause=2)
        except Exception as e:
            print("Error cargando grupo:", group_url, e)
            update_lastchecked_in_sheet(group_url)
            continue

        post_xpath = ".//div[.//div[@data-ad-rendering-role='story_message']]"
        # encontrar posts (heurística: role="article")
        try:
            posts = feed.find_elements(By.XPATH, post_xpath)
            if len(posts)<posts_per_group:
                scroll_and_load_group(driver, max_scrolls=2, pause=2)
                feed = driver.find_element(By.CSS_SELECTOR, "div[role='feed']")
                posts = feed.find_elements(By.XPATH, post_xpath)
        except Exception:
            posts = []

        posts_processed_in_group = 0
        total_processed = 0
        for post in posts:
            if posts_processed_in_group >= posts_per_group or processed_count >= limit_total:
                break

            try:
                # time.sleep(random.uniform(delay_between_posts_min, delay_between_posts_max))

                result= extract_post_data(post)

                if not result["phones"]:
                    total_processed += 1
                    print("Skip teléfono inválido en post", result["profile_url"])
                    continue
                
                primary=result['phones'][0]
                post_url=result['profile_url']
                # validación mínima teléfono
                if not primary or len(primary) < 8:
                    print("Skip: teléfono inválido:", primary, "en post", post_url)
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
                required_headers = ["Business","Name","Phone","SecondaryPhone","Status","Group","Timestamp","NextContact","City","URL","Email", "Channel", "PostText"]
                if not all(h in header_map for h in required_headers):
                    ws_pros.clear()
                    ws_pros.append_row(required_headers, value_input_option="USER_ENTERED")
                    header_map = {h: i+1 for i, h in enumerate(required_headers)}

                today_iso = datetime.now().date().isoformat()
                row = [""] * len(header_map)
                # asigna valores según header_map
                row[header_map["Business"]-1] = ""           # opcional, difícil de extraer reliably
                row[header_map["Name"]-1] = result["author"]
                row[header_map["Phone"]-1] = primary
                if len(result["phones"])>1:
                    row[header_map["SecondaryPhone"]-1] = result["phones"][1]
                row[header_map["Status"]-1] = "Nuevo"
                row[header_map["Group"]-1] = group['name']
                row[header_map["Timestamp"]-1] = today_iso
                row[header_map["NextContact"]-1] = ""
                row[header_map["City"]-1] = ""            # extraer ciudad es opcional/heurístico
                row[header_map["URL"]-1] = post_url
                row[header_map["Email"]-1] = ""             # intentar extraer con regex si quieres
                row[header_map["Channel"]-1] = "FB"
                row[header_map["PostText"]-1] = result["post_text"]

                # append a sheet
                ws_pros.append_row(row, value_input_option="USER_ENTERED")

                # marcar dedupe
                seen_phones.add(primary)
                existing_tels.add(primary)
                processed_count += 1
                posts_processed_in_group += 1
                total_processed += 1

                print(f"[GUARDADO] {primary}  ({result["author"]}) -> {post_url}  (total guardados: {processed_count})")

            except Exception as e:
                print(f"Error procesando post: {post_url} ", e)
                #Si hay error al procesar el post, no se cuenta
                total_processed += 1
                # posts_processed_in_group += 1
                continue

        # actualización LastChecked del grupo procesado
        update_lastchecked_in_sheet(group_url)
        print(f"Grupo comletado {group['name']} con {total_processed} posts procesados")
        # delay entre grupos
        if processed_count < limit_total:
            sleep_t = random.uniform(delay_between_groups_min, delay_between_groups_max)
            print(f"Delay {sleep_t:.1f}s antes del siguiente grupo...")
            time.sleep(sleep_t)

    # fin de run: resumen
    print("=== RUN COMPLETADO ===")
    print("Guardados totales:", processed_count)
    print("Terminado a:", datetime.now().isoformat())

def extract_post_data(post):
    """
    Recibe: post (selenium WebElement) correspondiente a un contenedor de publicación.
    Devuelve: dict con keys:
      - author (str)
      - profile_url (str)  (completa con https://www.facebook.com si viene relativa)
      - post_url (str)
      - post_text (str)
      - post_html (str)
      - PostText (str)
      - phones (list of normalized phones, puede quedar vacío)
    """
    result = {
        "author": "",
        "profile_url": "",
        "post_url": "",
        "post_text": "",
        "post_html": "",
        "post_text": "",
        "phones": []
    }

    try:
        # 1) texto del post (contenedor story_message)
        try:
            msg_el = post.find_element(By.XPATH, ".//div[@data-ad-rendering-role='story_message']")
            post_text = msg_el.text or ""
            post_html = msg_el.get_attribute("outerHTML") or ""
        except Exception:
            post_text = ""
            post_html = ""

        result["post_text"] = post_text
        result["post_html"] = post_html

        post_text = expand_post_and_get_text(post)

        # 2) autor y link al perfil (buscar anchor dentro de profile_name que contenga /user/ o /profile.php)
        try:
            author_anchor = post.find_element(
                By.XPATH,
                ".//div[@data-ad-rendering-role='profile_name']//a[contains(@href,'/user/') or contains(@href,'/profile.php')]"
            )
            href = author_anchor.get_attribute("href") or ""
            name = (author_anchor.text or "").strip()

            # Si el anchor contiene sub-elements y name salió vacío, intentar buscar b/span dentro
            if not name:
                try:
                    name = author_anchor.find_element(By.XPATH, ".//b//span").text.strip()
                except Exception:
                    name = (author_anchor.text or "").strip()

            # normalizar href relativo -> absoluto
            if href and href.startswith("/"):
                href = "https://www.facebook.com" + href.split("?")[0]
            else:
                href = href.split("?")[0] if href else ""

            result["author"] = name
            result["profile_url"] = href
        except Exception:
            # no se pudo extraer author/profile; dejar vacío y seguir
            result["author"] = ""
            result["profile_url"] = ""

        # 3) intentar encontrar permalink al post (anchors con /posts/ o permalink o story.php)
        post_url = ""
        try:
            anchors = post.find_elements(By.TAG_NAME, "a")
            for a in anchors:
                href = a.get_attribute("href") or ""
                href_clean = href.split("?")[0].rstrip("/")
                post_url = href_clean
                
            # fallback: muchas publicaciones tienen un link en el elemento de fecha/tiempo
            if not post_url:
                try:
                    time_link = post.find_element(By.XPATH, ".//a[contains(@href,'/posts/') or contains(@href,'/permalink') or contains(@href,'/story.php')]")
                    post_url = (time_link.get_attribute("href") or "").split("?")[0].rstrip("/")
                except Exception:
                    post_url = ""
        except Exception:
            post_url = ""

        result["post_url"] = post_url or ""

        # 4) extraer teléfonos usando tu extractor (buscar en texto y en html)
        phones_raw = []
        try:
            phones_raw.extend(extract_phones(post_text, post_html))
            # también buscar dentro del outerHTML del post por si wa.me o phone= aparecen fuera del story_message
            try:
                full_html = post.get_attribute("outerHTML") or ""
                phones_raw.extend(extract_phones("", full_html))
            except Exception:
                pass
        except Exception:
            phones_raw = []

        # normalizar y dedupe
        normalized = []
        for p in phones_raw:
            try:
                np = normalize_phone(p)
                if np and np not in normalized:
                    normalized.append(np)
            except Exception:
                continue

        result["post_text"] = post_text
        result["phones"] = normalized

    except Exception as e:
        # En caso de error no crítico, devolvemos lo que tengamos; loguea si quieres
        print("Warning: error extrayendo datos del post:", e)

    return result

def expand_post_and_get_text(post, min_wait=1.5, max_wait=3.5, max_click_attempts=2):
    """
    Intenta encontrar y clicar el botón "Ver más" / "See more" DENTRO del WebElement `post`,
    espera un pequeño tiempo aleatorio y devuelve (post_text, post_html, expanded_flag).
    - post: WebElement del post (nodo raíz que contiene story_message).
    - driver: instancia Selenium WebDriver (necesaria para ejecutar JS scroll/click).
    - min_wait/max_wait: rango para sleep aleatorio después del click.
    - max_click_attempts: cuántas veces intentamos hacer click si falla la primera vez.

    Retorna:
      (post_text: str, post_html: str, expanded: bool)
    """
    # Primero obtener el texto actual del contenedor del mensaje (si existe)
    try:
        msg_el = post.find_element(By.XPATH, ".//div[@data-ad-rendering-role='story_message']")
        pre_text = msg_el.text or ""
        pre_html = msg_el.get_attribute("outerHTML") or ""
    except NoSuchElementException:
        # No hay contenido textual principal => devolvemos vacíos
        return ("")
    except StaleElementReferenceException:
        # Elemento stale: intentar refrescar
        try:
            time.sleep(0.2)
            msg_el = post.find_element(By.XPATH, ".//div[@data-ad-rendering-role='story_message']")
            pre_text = msg_el.text or ""
            pre_html = msg_el.get_attribute("outerHTML") or ""
        except Exception:
            return ("")

    # XPaths heurísticos para encontrar el botón "ver más" / "see more" DENTRO del post
    candidate_xpaths = [
        ".//span[contains(text(),'Ver más')]",
        ".//span[contains(text(),'ver más')]",
        ".//a[contains(text(),'Ver más')]",
        ".//a[contains(text(),'ver más')]",
        ".//span[contains(text(),'See more')]",
        ".//a[contains(text(),'See more')]",
        ".//div[@role='button' and contains(., 'Ver más')]",
        ".//div[@role='button' and contains(., 'ver más')]",
        ".//div[@role='button' and contains(., 'See more')]",
        ".//a[@role='button' and contains(., 'Ver más')]",
        ".//a[@role='button' and contains(., 'See more')]",
    ]

    see_more_el = None
    for xp in candidate_xpaths:
        try:
            els = post.find_elements(By.XPATH, xp)
            if not els:
                continue
            # elegir el primer visible
            for el in els:
                try:
                    if el.is_displayed():
                        see_more_el = el
                        break
                except StaleElementReferenceException:
                    continue
            if see_more_el:
                break
        except Exception:
            continue

    if not see_more_el:
        # no hay "ver más" en este post
        return (pre_text)

    # Si encontramos el elemento, intentamos hacer scroll y click (varias estrategias)
    clicked = False
    attempts = 0
    while attempts < max_click_attempts and not clicked:
        attempts += 1
        try:
            # scroll al elemento centrado en pantalla
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", see_more_el)
            time.sleep(0.2)  # breve pausa tras el scroll

            # intentar click via JS para mayor fiabilidad
            driver.execute_script("arguments[0].click();", see_more_el)
            clicked = True
        except (ElementClickInterceptedException, StaleElementReferenceException, Exception):
            # fallback: intentar click nativo si JS falla (otra vez con scroll)
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", see_more_el)
                time.sleep(0.15)
                see_more_el.click()
                clicked = True
            except Exception:
                # re-intentar tras pequeña espera
                time.sleep(0.3 + 0.2 * attempts)

    if not clicked:
        # no pudimos clicar, devolvemos lo que teníamos
        return (pre_text)

    # esperar contenido cargue / expandido
    time.sleep(random.uniform(min_wait, max_wait))

    # re-obtener el elemento del mensaje (puede haber cambiado el DOM)
    try:
        # Intentar varias veces si hay stale reference
        new_text = pre_text
        new_html = pre_html
        for _ in range(4):
            try:
                msg_el = post.find_element(By.XPATH, ".//div[@data-ad-rendering-role='story_message']")
                new_text = msg_el.text or ""
                new_html = msg_el.get_attribute("outerHTML") or ""
                # si el texto cambió (por ejemplo se expandió), rompemos
                if new_text and new_text != pre_text:
                    break
                # si no cambió, esperamos un poco y reintentamos
                time.sleep(0.2)
            except StaleElementReferenceException:
                time.sleep(0.15)
                continue
    except Exception:
        # no crítico: retornar lo que tengamos
        return (pre_text)

    # Si llegamos aquí devolvemos el texto/html actualizados y flag True
    return (new_text)

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

def send_message():
    #Recuperar los mensajes activos
    gc = gspread.service_account(filename=service_account_json)
    sh = gc.open_by_key(spreadsheet_key)

    try:
        ws_messages = sh.worksheet("Messages")
    except gspread.WorksheetNotFound:
        raise RuntimeError("La hoja de mensajes no existe.")

    messages = ws_messages.get_all_records()
    active_messages = []
    for msg in messages:
        if str(msg.get("Active", "")).strip().upper() in("TRUE", "SI", "YES", "1"):
            active_messages.append({"id": msg.get("ID", ""), "message": msg.get("Message", ""), "sended": msg.get("Sended", "")})
    
    #Normalizar valores vacíos de sended
    for msg in active_messages:
        if msg["sended"] == "" or msg["sended"] is None:
            msg["sended"] = 0
        else:
            msg["sended"] = int(msg["sended"])
            
    #Recuperar los prospectos
    try:
        ws_prospects = sh.worksheet("Prospects")
    except gspread.WorksheetNotFound:
        raise RuntimeError("La hoja de prospectos no existe.")

    prospects = ws_prospects.get_all_records()
    prospects_list = []
    for p in prospects:
        if str(p.get("Status", "")).strip().upper() in("NUEVO"):
            prospects_list.append({"name": p.get("Name", ""), "phone": p.get("Phone", ""), "date": p.get("Timestamp", ""), "walink": ""})
            
    #Enviar mensaje
    for p in prospects_list:
        #Generar link para abrir chat
        link = f"https://web.whatsapp.com/send/?phone={p["phone"]}&text&type=phone_number&app_absent=0&utm_campaign=wa_api_send"
        driver.get(link)
        p["walink"] = link
    
        # Encontrar el valor mínimo de enviados
        min_sended = min(msg["sended"] for msg in active_messages)

        # Filtrar solo los que tienen ese mínimo
        candidates = [msg for msg in active_messages if msg["sended"] == min_sended]

        # Elegir aleatorio entre los mensajes y construir texto
        chosen = random.choice(candidates)
        name = p["name"] or ""
        message = chosen["message"].replace("name", name)

        if not message:
            print("Error al intentar crear el mensaje, revisa el documento de mensajes")
            continue

        #Seleccionar input por xpath
        WebDriverWait(driver, 5).until(EC.presence_of_all_elements_located((By.XPATH, '//*[@id="main"]/footer/div[1]/div/span/div/div[2]/div/div[3]/div[1]')))
        time.sleep(2)
        elem = driver.find_element(By.XPATH, '//*[@id="main"]/footer/div[1]/div/span/div/div[2]/div/div[3]/div[1]')
        
        #Enviar mensaje
        if paste_edit_and_send(elem, message):
            try:
                #Cambiar estado del prospecto fecha de seguimiento y sacar de la lista
                cell = ws_prospects.find(str(p["phone"]))
                if cell:
                    row = cell.row
                    headers = ws_prospects.row_values(1)
                    try:
                        stc_indx = headers.index("Status") + 1
                        ncc_indx = headers.index("NextContact") + 1
                    except ValueError:
                        stc_indx = 5
                        ncc_indx = 8
                    ws_prospects.update_cell(row, stc_indx, "Apertura")
                    ws_prospects.update_cell(row, ncc_indx, (datetime.now() + timedelta(days=2)).date().isoformat())
            except Exception as e:
                print("Warning: no se pudo actualizar estado para: ", p["phone"], ":", e)
        else:
            print("No se pudo enviar mensaje a prospecto: ", p["phone"])

        #Aumentar mensaje enviado
        chosen["sended"] += 1

    #Actualizar información de los mensajes
    for msg in active_messages:
        cell = ws_messages.find(str(msg["id"]))
        if cell:
            row = cell.row
            headers = ws_messages.row_values(1)
            try:
                sc_indx = headers.index("Sended") + 1
            except ValueError:
                sc_indx = 3
            ws_messages.update_cell(row, sc_indx, msg["sended"])

def paste_edit_and_send(input_el, message,
                        min_final_pause=0.6, max_final_pause=1.6,
                        char_delay=(0.02, 0.06),
                        wait_timeout=10):
    """
    Pega `message` en `input_el` (WebElement contenteditable), realiza 1..N ediciones
    aleatorias (backspace + retype) y hace click en el botón de enviar.
    - input_el: WebElement del input (ej. div[contenteditable="true"])
    - message: texto a pegar
    - edits_range: tupla (min_edits, max_edits) cantidad de ediciones aleatorias a hacer
    - char_delay: tupla (min,max) delay entre caracteres cuando re-escribimos fragmentos
    - wait_timeout: tiempo para esperar selectores del botón de enviar
    Retorna True si aparentemente se envió el mensaje (botón clickeado), False si falló.
    """

    # 1) intentar poner el texto en el clipboard (pyperclip preferido, si no navigator.clipboard)
    tried_clipboard = False
    try:
        clip.copy(message)
        tried_clipboard = True
    except Exception as e:
        print(e)
        # fallback: intentar usar navigator.clipboard (puede fallar en algunos entornos)
        try:
            driver.execute_script("return navigator.clipboard && navigator.clipboard.writeText(arguments[0]);", message)
            tried_clipboard = True
        except Exception:
            tried_clipboard = False

    # 2) enfocar el input
    try:
        input_el.click()
    except Exception:
        try:
            driver.execute_script("arguments[0].focus();", input_el)
        except Exception:
            pass
    time.sleep(random.uniform(0.08, 0.18))

    # 3) pegar (usar la tecla adecuada según OS)
    sysname = platform.system()
    paste_keys = (Keys.CONTROL, "v")
    if sysname == "Darwin":
        paste_keys = (Keys.COMMAND, "v")

    pasted = False
    if tried_clipboard:
        try:
            #borra contenido previo
            input_el.send_keys(Keys.CONTROL, "a")
            time.sleep(0.03)
            input_el.send_keys(Keys.DELETE)
            time.sleep(0.03)
            # pegar Ctrl/Cmd+V
            input_el.send_keys(*paste_keys)
            time.sleep(random.uniform(0.06, 0.18))
            pasted = True
        except Exception:
            pasted = False

    # 4) si pegar no funcionó, fallback: escribir el texto completo (más lento)
    if not pasted:
        try:
            # selecciona todo y borra (asegura input limpio)
            try:
                input_el.send_keys(Keys.CONTROL + "a")
                time.sleep(0.03)
                input_el.send_keys(Keys.BACKSPACE)
            except Exception:
                pass
            for ch in message:
                input_el.send_keys(ch)
                time.sleep(random.uniform(*char_delay))
            time.sleep(0.05)
            pasted = True
        except Exception:
            # no se pudo escribir/pastear: abortar
            return False

    # 5) decidir si editar
    edit = random.randint(0,1)
    if edit == 1:
        # 6) pequeñas ediciones aleatorias de 1 a 3 palabras
        num_steps = random.randint(2, 4)
        #mover el cursor al final de texto
        input_el.send_keys(Keys.END)
        #mover el cursor n veces
        for s in range(num_steps):
            input_el.send_keys(Keys.CONTROL + Keys.SHIFT + Keys.LEFT)
            time.sleep(random.uniform(*char_delay))
            
        try:
            #leer la palabra seleccionada
            selected = driver.execute_script("return window.getSelection().toString();")
            #si len(selected.strip()) >= 3 borrar y reescribir
            if len(selected.strip()) >= 3:
                input_el.send_keys(Keys.DELETE)
                time.sleep(random.uniform(*char_delay))
                #reescribir
                for ch in selected:
                    input_el.send_keys(ch)
                    time.sleep(random.uniform(*char_delay))
            else:
                #seleccionar una palabra más
                input_el.send_keys(Keys.CONTROL + Keys.SHIFT + Keys.LEFT)
                time.sleep(random.uniform(*char_delay))
                selected = driver.execute_script("return window.getSelection().toString();")
                if len(selected.strip()) >= 3:
                    input_el.send_keys(Keys.DELETE)
                    #reescribir
                    for ch in selected:
                        input_el.send_keys(ch)
                        time.sleep(random.uniform(*char_delay))
        except Exception:
            #dejar sin editar
            pass
        
    # 7) pequeña pausa final antes de enviar (simula reflexión)
    time.sleep(random.uniform(min_final_pause, max_final_pause))

    # 8) localizar el botón de enviar con varias estrategias
    send_selectors = [
        (By.CSS_SELECTOR, 'button[aria-label="Send"]'),
        (By.CSS_SELECTOR, 'button[aria-label="Enviar"]'),
        (By.CSS_SELECTOR, 'span[data-icon="send"]'),
        (By.CSS_SELECTOR, 'div[data-testid="compose-btn-send"]'),
        (By.XPATH, "//button[contains(., 'Send') or contains(., 'Enviar')]"),
    ]

    send_el = None
    for by, sel in send_selectors:
        try:
            send_el = WebDriverWait(driver, wait_timeout).until(EC.element_to_be_clickable((by, sel)))
            if send_el:
                break
        except Exception:
            send_el = None
            continue

    if not send_el:
        # No encontramos botón de enviar: intentar enviar con ENTER (como fallback)
        try:
            input_el.send_keys(Keys.ENTER)
            return True
        except Exception:
            return False

    # 8) click robusto en el botón de enviar
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", send_el)
        time.sleep(random.uniform(0.05, 0.12))
        driver.execute_script("arguments[0].click();", send_el)
    except Exception:
        try:
            send_el.click()
        except Exception:
            # fallback: intentar presionar ENTER en el input
            try:
                input_el.send_keys(Keys.ENTER)
            except Exception:
                return False

    # 9) esperar confirmación mínima: que el mensaje aparezca en la UI (heurística simple)
    # Buscamos en la vista de mensajes algún elemento que contenga parte del texto enviado (últimos burbujas)
    confirm = False
    try:
        snippet = (message[:20].strip() or " ")  # usar un fragmento para búsqueda
        # esperar a que aparezca una burbuja con ese snippet (timeout corto)
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.XPATH, f"//div[contains(., {repr(snippet)}) and (contains(@class,'message') or contains(@data-testid,'msg'))]"))
        )
        confirm = True
        time.sleep(random.uniform(3,5))
    except Exception:
        # si no confirmamos, consideramos enviado (no 100% fiable) — el caller puede implementar re-checks
        confirm = True

    return confirm

# Ejecución main:
if __name__ == "__main__":
    driver = iniciar_navegador(headless=False)
    # driver.get("https://www.facebook.com")
    cfg = load_config()
    spreadsheet_key = cfg["SPREADSHEET_KEY"]
    service_account_json = cfg["SERVICE_ACCOUNT_PATH"]
    # scrape_groups_to_sheet()
    # scrape_posts()
    send_message()
    input("Presiona Enter para cerrar el navegador...")
    driver.quit()
