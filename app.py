from flask import Flask, request, jsonify
import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import pandas as pd
import geopandas as gpd
import io
import re
from pykml import parser
from shapely.geometry import Polygon
import networkx as nx
from collections import defaultdict
import pulp
import os

app = Flask(__name__)

@app.route('/optimizar', methods=['POST'])
def ejecutar_optimizacion():
    try:
        # 1. Recibir tu "gafete" desde Google Sheets
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Falta token de autorización"}), 401
            
        token = auth_header.split(' ')[1]
        
        # 2. Iniciar sesión usando tu token para saltar bloqueos
        creds = Credentials(token)
        gc = gspread.authorize(creds)
        
        # 3. Conectar a tu hoja
        SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1iGalLyJw6ktOQ1jpq1X-hTP13Pd7q0NMG6kPoFssZKg/edit'
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        sheet_summary = spreadsheet.worksheet('Summary')
        
        # 4. Descargar el KML 
        kml_url = sheet_summary.acell('H3').value
        kml_id = kml_url.split('/d/')[1].split('/')[0]
        drive_service = build('drive', 'v3', credentials=creds)
        
        request_drive = drive_service.files().get_media(fileId=kml_id)
        descarga = io.BytesIO()
        descarga.write(request_drive.execute())
        
        with open('areas_local.kml', 'wb') as f:
            f.write(descarga.getvalue())
            
        # =========================================================
        # (En el siguiente paso pegaremos aquí tu modelo matemático)
        # =========================================================
        
        return jsonify({"mensaje": "Balanceo exitoso"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
