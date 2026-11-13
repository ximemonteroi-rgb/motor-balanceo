from flask import Flask, request, jsonify
import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import pandas as pd
import geopandas as gpd
import traceback
import io
import re
from shapely.geometry import Polygon
from shapely.validation import make_valid
from collections import defaultdict
import pulp
import os

app = Flask(__name__)

@app.route('/optimizar', methods=['POST'])
def ejecutar_optimizacion():
    try:
        # =========================================================
        # 1. AUTENTICACIÓN Y CONEXIÓN CON IDENTIDAD DEL USUARIO
        # =========================================================
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Falta token de autorización"}), 401
            
        token = auth_header.split(' ')[1]
        creds = Credentials(token)
        gc = gspread.authorize(creds)
        
        SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1iGalLyJw6ktOQ1jpq1X-hTP13Pd7q0NMG6kPoFssZKg/edit'
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        sheet_summary = spreadsheet.worksheet('Summary')
        
        # =========================================================
        # 2. LECTURA DE DATOS (SHEETS)
        # =========================================================
        escenario = sheet_summary.acell('B10').value
        bd_implicado = sheet_summary.acell('C10').value
        
        valores_hoja = sheet_summary.get_all_values()
        df_crudo = pd.DataFrame(valores_hoja)
        
        # Buscar fila de encabezados dinámicamente
        fila_encabezados = None
        for i, fila in df_crudo.iterrows():
            if 'BD Area ID' in fila.values:
                fila_encabezados = i
                break
                
        if fila_encabezados is None:
            return jsonify({"error": "No se encontró la columna 'BD Area ID' en Sheets"}), 400
            
        df = pd.DataFrame(df_crudo.values[fila_encabezados+1:], columns=df_crudo.iloc[fila_encabezados])
        df.columns = df.columns.str.strip()
        df = df[df['BD Area ID'].str.strip() != '']
        
        df['BD Area ID'] = df['BD Area ID'].astype(str).str.strip()
        df['Rs 1'] = pd.to_numeric(df['Rs 1'], errors='coerce').fillna(0)
        total_shops = df['Rs 1'].sum()
        
        bds_actuales = df['BD Actual'].dropna().unique().tolist()
        bd_baja = None
        
        # Configurar Bolsa de BDs según escenario
        if escenario == 'Distribution with current HC' or not escenario:
            bds_elegibles = bds_actuales
        elif escenario == 'New Hire':
            if bd_implicado and bd_implicado not in bds_actuales and bd_implicado != "Escribe el nombre aquí...":
                bds_elegibles = bds_actuales + [bd_implicado.strip()]
            else:
                bds_elegibles = bds_actuales
        elif escenario == 'Termination':
            if bd_implicado and bd_implicado in bds_actuales:
                bds_elegibles = [bd for bd in bds_actuales if bd != bd_implicado.strip()]
                bd_baja = bd_implicado.strip()
            else:
                bds_elegibles = bds_actuales
        
        TARGET = total_shops / len(bds_elegibles) if len(bds_elegibles) > 0 else 0

        # =========================================================
        # 3. DESCARGA Y CRUCE DE KML (MÉTODO UNIVERSAL)
        # =========================================================
        kml_url = sheet_summary.acell('H3').value
        kml_id = kml_url.split('/d/')[1].split('/')[0]
        
        drive_service = build('drive', 'v3', credentials=creds)
        request_drive = drive_service.files().get_media(fileId=kml_id)
        
        descarga = io.BytesIO()
        downloader = MediaIoBaseDownload(descarga, request_drive)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            
        content = descarga.getvalue().decode('utf-8')
        
        blocks = re.split(r'<[Pp]lacemark>', content)
        placemarks_data = []

        for block in blocks[1:]:
            id_match = re.search(r'<Data name="bd_area_id">\s*<value>(.*?)</value>', block, re.IGNORECASE | re.DOTALL)
            raw_id = id_match.group(1) if id_match else ""
            clean_id = re.sub(r'[^0-9]', '', raw_id)

            name_match = re.search(r'<Data name="bd_area_name">\s*<value>(.*?)</value>', block, re.IGNORECASE | re.DOTALL)
            if not name_match:
                name_match = re.search(r'<name>(.*?)</name>', block, re.IGNORECASE)
            raw_name = name_match.group(1) if name_match else ""
            clean_name = re.sub(r'[^0-9]', '', raw_name)

            coords = []
            coords_match = re.search(r'<coordinates>(.*?)</coordinates>', block, re.IGNORECASE | re.DOTALL)
            if coords_match:
                coord_text = coords_match.group(1).strip()
                for pair in coord_text.split():
                    parts = pair.split(',')
                    if len(parts) >= 2:
                        try:
                            coords.append((float(parts[0]), float(parts[1])))
                        except ValueError:
                            pass

            if len(coords) >= 3 and (clean_id or clean_name):
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                placemarks_data.append({
                    'KML_ID': clean_id, 
                    'KML_Name_Num': clean_name, 
                    'geometry': Polygon(coords)
                })

        gdf_kml = gpd.GeoDataFrame(placemarks_data, crs='EPSG:4326')

        df['Sheets_ID'] = df['BD Area ID'].astype(str).apply(lambda x: re.sub(r'[^0-9]', '', x))
        df['Sheets_Name_Num'] = df['BD Area'].astype(str).apply(lambda x: re.sub(r'[^0-9]', '', x))

        gdf = gdf_kml.merge(df, left_on='KML_ID', right_on='Sheets_ID', how='inner')
        if len(gdf) == 0:
            gdf = gdf_kml.merge(df, left_on='KML_Name_Num', right_on='Sheets_Name_Num', how='inner')
            
        if len(gdf) == 0:
            return jsonify({"error": "Cruce fallido. Los IDs/Nombres del mapa no coinciden con Sheets."}), 400

        # =========================================================
        # 4. SANACIÓN Y CÁLCULO DE VECINOS
        # =========================================================
        gdf['geometry'] = gdf['geometry'].apply(lambda geom: make_valid(geom) if not geom.is_valid else geom)
        gdf_proj = gdf.to_crs(epsg=3857).reset_index(drop=True)
        gdf_proj['geom_buf'] = gdf_proj.geometry.buffer(50)
        sindex = gdf_proj['geom_buf'].sindex

        neighbor_pairs = []
        for idx, row in gdf_proj.iterrows():
            cands = list(sindex.intersection(row['geom_buf'].bounds))
            for j in cands:
                if j > idx:
                    if row['geom_buf'].intersects(gdf_proj.at[j, 'geom_buf']):
                        neighbor_pairs.append((row['BD Area ID'], gdf_proj.at[j, 'BD Area ID']))

        # =========================================================
        # 5. DEFINICIÓN DINÁMICA DE RAÍCES
        # =========================================================
        roots = {}
        if escenario != 'Distribution with current HC':
            for bd in bds_elegibles:
                if escenario == 'New Hire' and bd_implicado and bd == bd_implicado.strip():
                    continue
                areas_bd = df[df['BD Actual'] == bd].sort_values('Rs 1', ascending=False)
                if not areas_bd.empty:
                    roots[bd] = areas_bd.iloc[0]['BD Area ID']
            
            if escenario == 'New Hire' and bd_implicado:
                bd_new = bd_implicado.strip()
                bd_sobrado = df.groupby('BD Actual')['Rs 1'].sum().idxmax()
                areas_sobrado = df[df['BD Actual'] == bd_sobrado].sort_values('Rs 1', ascending=False)
                if not areas_sobrado.empty:
                    roots[bd_new] = areas_sobrado.iloc[0]['BD Area ID']
                    if len(areas_sobrado) > 1:
                        roots[bd_sobrado] = areas_sobrado.iloc[1]['BD Area ID']

        # =========================================================
        # 6. MOTOR MATEMÁTICO (FUNCIÓN SOLVE)
        # =========================================================
        def solve_scenario(w1, w2, nombre_escenario=""):
            prob = pulp.LpProblem("Balanceo", pulp.LpMinimize)
            nodos = [str(a).strip() for a in df['BD Area ID'].unique()]
            equipos = [str(b).strip() for b in bds_elegibles]
            pares_limpios = [(str(i).strip(), str(j).strip()) for i, j in neighbor_pairs]

            dict_vecinos = defaultdict(list)
            for i, j in pares_limpios:
                dict_vecinos[i].append(j)
                dict_vecinos[j].append(i)

            roots_clean = {str(k).strip(): str(v).strip() for k, v in roots.items()}

            x_keys = [(a, b) for a in nodos for b in equipos]
            x = pulp.LpVariable.dicts("x", x_keys, cat='Binary')
            dev = pulp.LpVariable.dicts("dev", equipos, lowBound=0)
            move = pulp.LpVariable.dicts("move", x_keys, cat='Binary')

            for a in nodos:
                prob += pulp.lpSum(x[(a, b)] for b in equipos) == 1

            TOL = 0.99 if escenario == 'Distribution with current HC' else 0.20
            LOW = TARGET * (1 - TOL)  
            HIGH = TARGET * (1 + TOL) 

            for b in equipos:
                total_b = pulp.lpSum(
                    df.loc[df['BD Area ID'].astype(str).str.strip() == a, 'Rs 1'].iat[0] * x[(a, b)] 
                    for a in nodos
                )
                prob += total_b >= LOW   
                prob += total_b <= HIGH  
                prob += dev[b] >= total_b - TARGET
                prob += dev[b] >= TARGET - total_b

            for a in nodos:
                bd_actual_raw = df.loc[df['BD Area ID'].astype(str).str.strip() == a, 'BD Actual'].iat[0]
                bd_actual_a = str(bd_actual_raw).strip()
                for b in equipos:
                    if escenario == 'Termination' and bd_baja and bd_actual_a == str(bd_baja).strip():
                        prob += move[(a, b)] == 0
                    else:
                        es_orig = 1 if bd_actual_a == b else 0
                        prob += move[(a, b)] >= x[(a, b)] - es_orig
                        prob += move[(a, b)] >= es_orig - x[(a, b)]

            max_flow = len(nodos)
            f_vars = [(i, j, b) for i, j in pares_limpios for b in equipos] + \
                     [(j, i, b) for i, j in pares_limpios for b in equipos]
            f = pulp.LpVariable.dicts("f", f_vars, lowBound=0, upBound=max_flow)

            for i, j in pares_limpios:
                for b in equipos:
                    prob += f[(i, j, b)] <= max_flow * x[(i, b)]
                    prob += f[(i, j, b)] <= max_flow * x[(j, b)]
                    prob += f[(j, i, b)] <= max_flow * x[(j, b)]
                    prob += f[(j, i, b)] <= max_flow * x[(i, b)]

            for b in equipos:
                if b in roots_clean:
                    r = roots_clean[b]
                    prob += x[(r, b)] == 1 
                    prob += (pulp.lpSum(f[(r, j, b)] for j in dict_vecinos[r]) -
                             pulp.lpSum(f[(j, r, b)] for j in dict_vecinos[r])) == pulp.lpSum(x[(a, b)] for a in nodos) - 1
                    for a in nodos:
                        if a != r:
                            prob += (pulp.lpSum(f[(i, a, b)] for i in dict_vecinos[a]) -
                                     pulp.lpSum(f[(a, j, b)] for j in dict_vecinos[a])) == x[(a, b)]
                else:
                    sink_vars = pulp.LpVariable.dicts(f"sink_{b}", nodos, cat='Binary')
                    prob += pulp.lpSum(sink_vars[a] for a in nodos) == 1
                    for a in nodos:
                        prob += sink_vars[a] <= x[(a, b)]
                        prob += (pulp.lpSum(f[(i, a, b)] for i in dict_vecinos[a]) -
                                 pulp.lpSum(f[(a, j, b)] for j in dict_vecinos[a])) >= x[(a, b)] - max_flow * sink_vars[a]
                        prob += (pulp.lpSum(f[(i, a, b)] for i in dict_vecinos[a]) -
                                 pulp.lpSum(f[(a, j, b)] for j in dict_vecinos[a])) <= x[(a, b)] + max_flow * sink_vars[a]

            dev_shops_sum = pulp.lpSum(dev[b] for b in equipos)
            move_cost_sum = pulp.lpSum(move[(a, b)] for a in nodos for b in equipos)
            prob += w1 * dev_shops_sum + w2 * move_cost_sum

            solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=180)
            prob.solve(solver)
            
            if pulp.LpStatus[prob.status] != "Optimal":
                return None

            sol = []
            for a in nodos:
                for b in equipos:
                    if x[(a, b)].value() is not None and x[(a, b)].value() > 0.5:
                        id_orig = df.loc[df['BD Area ID'].astype(str).str.strip() == a, 'BD Area ID'].iat[0]
                        sol.append({'BD Area ID': id_orig, nombre_escenario: b})
            return pd.DataFrame(sol)

        # =========================================================
        # 7. EJECUCIÓN Y EXPORTACIÓN A GOOGLE SHEETS (SUMMARY)
        # =========================================================
        df_p1 = solve_scenario(1.0, 0.0, "Propuesta 1")
        df_p2 = solve_scenario(0.5, 0.5, "Propuesta 2")
        df_p3 = solve_scenario(0.1, 0.9, "Propuesta 3")

        df_export = df[['BD Area ID']].copy()
        df_export = df_export.merge(df_p1, on='BD Area ID', how='left') if df_p1 is not None else df_export.assign(**{'Propuesta 1': "Inviable"})
        df_export = df_export.merge(df_p2, on='BD Area ID', how='left') if df_p2 is not None else df_export.assign(**{'Propuesta 2': "Inviable"})
        df_export = df_export.merge(df_p3, on='BD Area ID', how='left') if df_p3 is not None else df_export.assign(**{'Propuesta 3': "Inviable"})

        # Asegúrate de reincorporar las columnas de propuestas al df principal para que el mapa las lea
        df['Propuesta 1'] = df_export['Propuesta 1']
        df['Propuesta 2'] = df_export['Propuesta 2']
        df['Propuesta 3'] = df_export['Propuesta 3']

        fila_inicio_datos = fila_encabezados + 2
        rango_limpiar = f'J{fila_inicio_datos}:L'
        sheet_summary.batch_clear([rango_limpiar])
        
        datos_exportar = df_export[['Propuesta 1', 'Propuesta 2', 'Propuesta 3']].fillna("Sin Asignar").values.tolist()
        sheet_summary.update(f'J{fila_inicio_datos}', datos_exportar)
        
        # =========================================================
        # 8. GENERAR MAPA Y SUBIR A DRIVE
        # =========================================================
        import folium
        from googleapiclient.http import MediaFileUpload
        import matplotlib.colors as mcolors

        # Sistema automático de cruce para el mapa (Fallback)
        gdf_final = gdf_kml.merge(df, left_on='KML_ID', right_on='Sheets_ID', how='inner')
        if len(gdf_final) == 0:
            gdf_final = gdf_kml.merge(df, left_on='KML_Name_Num', right_on='Sheets_Name_Num', how='inner')

        bds_todos = list(set(bds_elegibles + df['BD Actual'].dropna().unique().tolist()))
        bds_todos = [bd for bd in bds_todos if bd != 'Sin Asignar']

        colores_base = list(mcolors.TABLEAU_COLORS.values())
        color_dict = {bd: colores_base[i % len(colores_base)] for i, bd in enumerate(bds_todos)}
        color_dict['Sin Asignar'] = '#000000'

        # Calcular centro limpiamente
        centro_y = gdf_final.geometry.centroid.y.mean() if not gdf_final.empty else 0
        centro_x = gdf_final.geometry.centroid.x.mean() if not gdf_final.empty else 0

        # Usar un enlace de mapa directo para evadir baneos de OSM y marcas de agua de CartoDB
        tiles_url = 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'
        attr = '&copy; OpenStreetMap contributors &copy; CARTO'
        m = folium.Map(location=[centro_y, centro_x], zoom_start=12, tiles=tiles_url, attr=attr)

        fg_actual = folium.FeatureGroup(name='Estado Actual', show=False)
        fg_p1 = folium.FeatureGroup(name='Propuesta 1', show=True)
        fg_p2 = folium.FeatureGroup(name='Propuesta 2', show=False)
        fg_p3 = folium.FeatureGroup(name='Propuesta 3', show=False)

        def get_style(bd_name):
            return {
                'fillColor': color_dict.get(bd_name, '#000000'),
                'color': 'white',
                'weight': 1.5,
                'fillOpacity': 0.7
            }

        for idx, row in gdf_final.iterrows():
            base_tooltip = f"<b>Área:</b> {row['BD Area ID']}<br><b>Shops:</b> {row['Rs 1']}<br>"

            bd_actual = row.get('BD Actual', 'Sin Asignar')
            folium.GeoJson(row['geometry'], style_function=lambda x, bd=bd_actual: get_style(bd), tooltip=base_tooltip + f"<b>BD Asignado:</b> {bd_actual}").add_to(fg_actual)

            bd_p1 = row.get('Propuesta 1', 'Sin Asignar')
            folium.GeoJson(row['geometry'], style_function=lambda x, bd=bd_p1: get_style(bd), tooltip=base_tooltip + f"<b>Propuesta 1:</b> {bd_p1}").add_to(fg_p1)

            bd_p2 = row.get('Propuesta 2', 'Sin Asignar')
            folium.GeoJson(row['geometry'], style_function=lambda x, bd=bd_p2: get_style(bd), tooltip=base_tooltip + f"<b>Propuesta 2:</b> {bd_p2}").add_to(fg_p2)

            bd_p3 = row.get('Propuesta 3', 'Sin Asignar')
            folium.GeoJson(row['geometry'], style_function=lambda x, bd=bd_p3: get_style(bd), tooltip=base_tooltip + f"<b>Propuesta 3:</b> {bd_p3}").add_to(fg_p3)

        fg_actual.add_to(m)
        fg_p1.add_to(m)
        fg_p2.add_to(m)
        fg_p3.add_to(m)
        folium.LayerControl().add_to(m)

        # LEYENDA
        legend_html = '''
        <div style="position: fixed;
                    top: 150px; right: 10px; width: auto; max-height: 400px; overflow-y: auto;
                    border:2px solid grey; z-index:9999; font-size:14px;
                    background-color:white; padding: 10px;">
            <b>Owner Legend</b><br>
        '''
        for bd in bds_todos:
            color = color_dict.get(bd, '#000000')
            legend_html += f'<div style="margin-bottom:3px;"><i style="background:{color};width:15px;height:15px;float:left;margin-right:5px;border:1px solid black;"></i>{bd}</div>'
        legend_html += '</div>'

        m.get_root().html.add_child(folium.Element(legend_html))

        # Guardar y subir a Drive
        ciudad_seleccionada = sheet_summary.acell('C5').value
        if not ciudad_seleccionada:
            ciudad_seleccionada = "General"
        
        html_filename = f'Mapa Propuesta {ciudad_seleccionada}, {escenario}.html'
        m.save(html_filename)

        file_metadata = {
            'name': html_filename,
            'parents': ['1iHmZ5xyxcFrun4tEUvXaBES2RFRMwp0i']  
        }
        
        media = MediaFileUpload(html_filename, mimetype='text/html', resumable=True)
        uploaded_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()

        file_id = uploaded_file.get('id')
        direct_download_url = f'https://drive.google.com/uc?export=download&id={file_id}'

        try:
            drive_service.permissions().create(
                fileId=file_id,
                body={'type': 'domain', 'role': 'reader', 'domain': 'didi-labs.com'},
                supportsAllDrives=True
            ).execute()
        except Exception as perm_e:
            print(f"Advertencia al dar permisos de dominio: {perm_e}")

        formula_link = f'=HYPERLINK("{direct_download_url}", "🗺️ Abrir Mapa {ciudad_seleccionada}, {escenario}")'
        sheet_summary.update(values=[[formula_link]], range_name='H7', value_input_option='USER_ENTERED')        # =========================================================
        # 9. ACTUALIZAR HOJA "portfolio"
        # =========================================================
        try:
            sheet_portfolio = spreadsheet.worksheet('portfolio')
            portfolio_raw = sheet_portfolio.get_all_values()
            encabezados_port = portfolio_raw[0]

            df_port = pd.DataFrame(portfolio_raw[1:], columns=encabezados_port)

            map_p1 = {re.sub(r'[^0-9]', '', str(k)): v for k, v in zip(df['BD Area ID'], df['Propuesta 1'])}
            map_p2 = {re.sub(r'[^0-9]', '', str(k)): v for k, v in zip(df['BD Area ID'], df['Propuesta 2'])}
            map_p3 = {re.sub(r'[^0-9]', '', str(k)): v for k, v in zip(df['BD Area ID'], df['Propuesta 3'])}

            for idx, row in df_port.iterrows():
                area_id_raw = str(row.get('ID de BD Area', '')).strip()
                area_num = re.sub(r'[^0-9]', '', area_id_raw)
                if area_num in map_p1 and area_num != "":
                    df_port.at[idx, 'Propuesta 1'] = map_p1[area_num]
                    df_port.at[idx, 'Propuesta 2'] = map_p2[area_num]
                    df_port.at[idx, 'Propuesta 3'] = map_p3[area_num]

            col_inicio_idx = encabezados_port.index('Propuesta 1') + 1
            columnas_subir_port = ['Propuesta 1', 'Propuesta 2', 'Propuesta 3']
            datos_subir_port = df_port[columnas_subir_port].values.tolist()

            from gspread.utils import rowcol_to_a1
            rango_inicio_port = rowcol_to_a1(2, col_inicio_idx)
            rango_fin_port = rowcol_to_a1(1 + len(datos_subir_port), col_inicio_idx + 2)
            
            sheet_portfolio.update(values=datos_subir_port, range_name=f'{rango_inicio_port}:{rango_fin_port}')
        except Exception as port_e:
            print(f"Error actualizando la hoja portfolio: {port_e}")

        return jsonify({"mensaje": "Balanceo exitoso y Sheets actualizado"}), 200
        
    except Exception as e:
        import traceback
        error_detallado = traceback.format_exc()
        print("ERROR FATAL:\n", error_detallado)
        return jsonify({"error": error_detallado}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
