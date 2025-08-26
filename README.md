# Prospector FB – Scraper de Prospección con Selenium y Notion

## Descripción
**Prospector FB** es un proyecto desarrollado en Python que permite automatizar la prospección de negocios en Facebook.  
El scraper busca páginas de negocios según criterios definidos, extrae información relevante y la sincroniza con **Notion** para su seguimiento.  
Además, se integra con **WhatsApp Web** para automatizar envíos de mensajes de apertura y seguimiento.

---

## Funcionalidades (v1)
- Login automatizado a Facebook con Selenium (sesión persistente para no ingresar credenciales cada vez).  
- Extracción de información de páginas de negocios: nombre, URL, categoría, número de seguidores.  
- Guardado de prospectos en Notion con estado inicial “Nuevo” y fecha de seguimiento.  
- Preparado para integrarse con envío automático de mensajes por WhatsApp (pendiente de desarrollo completo).  

---

## Estructura del proyecto
prospector/
│
├─ drivers/ ← ChromeDriver si se usa manualmente
├─ data/ ← cookies, resultados CSV/JSON
├─ .env ← credenciales FB (no subir a GitHub)
├─ scraper.py ← código principal
├─ requirements.txt ← dependencias Python
└─ README.md ← este archivo

---

## Tecnologías utilizadas
- Python 3.11+  
- Selenium 4+  
- BeautifulSoup4 (para parsing HTML opcional)  
- Notion SDK (para guardar y actualizar prospectos)  
- WhatsApp Web JS / Selenium (para envíos automáticos de mensajes de apertura)

---

## Instalación y setup
1. Clonar este repositorio:
```bash
git clone https://github.com/tu_usuario/prospector.git
cd prospector
```

2. Crear y activar entorno virtual:
```bash
python3 -m venv .venv
source .venv/bin/activate
```
3. Instalar dependencias:
```bash
pip install -r requirements.txt
```

4. Configurar .env con tus credenciales de Facebook:
```bash
FB_USER=tu_correo
FB_PASS=tu_contraseña
```

5. Ejecutar scraper:
```bash
python scraper.py
```

**Estado del proyecto**

Esta es la primera versión, enfocada en el login y extracción de datos de páginas de Facebook.
Próximamente se integrará el envío automático de mensajes y manejo completo de seguimientos en Notion.

**Contribuciones**

Por ahora el proyecto es personal y parte de mi portafolio, pero se aceptan sugerencias y mejoras vía issues o pull requests.

**Autor**

Agustin Ruiz – GitHub