import os

#load_config
#Ruta del entorno
env_path: str = ".env"

#start_browser
#Iniciar navegador: False = sin ventana; True = con ventana
headless = False
#Directorio del perfil de chrome
profile_dir = os.path.expanduser("~/prospector/chrome-data")

#scrape_groups_to_sheet
#Grupos a revisar por cada iteración
groups_per_scrap = 5

limit_total = 50