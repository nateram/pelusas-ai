
import requests as http_requests
import json
import time
import threading
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import pelusas 
from rich.console import Console as RichConsole 

app = Flask(__name__)
socketio = SocketIO(app)
server_console = RichConsole() 

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = "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" # ¡Reemplazar o usar variable de entorno!

_human_decision_events = {}
_human_response_data = {}

class WebCallbackHandlerForHuman:
    def __init__(self, socketio_instance, sid):
        self.socketio = socketio_instance
        self.sid = sid
        # Asegurar que las entradas de sincronización se crean/limpian por SID
        _human_decision_events[self.sid] = threading.Event()
        _human_response_data[self.sid] = {}

    def _cleanup_on_error_or_timeout(self):
        server_console.log(f"[WebCallbackHandler] Cleaning up for SID: {self.sid}")
        _human_decision_events.pop(self.sid, None)
        _human_response_data.pop(self.sid, None)

    def request_human_action(self, action_type, details):
        server_console.log(f"[WebCallbackHandler] Requesting action '{action_type}' from SID: {self.sid}, Details: {details}")
        
        if self.sid not in _human_decision_events or self.sid not in _human_response_data:
            server_console.log(f"[WebCallbackHandler] SID {self.sid} no longer active or sync objects missing. Returning default for {action_type}.")
            if action_type == "confirm_rules": return False
            if action_type == "decide_action": return "plantarse", 0.0 # (decision_str, tiempo_float)
            if action_type == "decide_steal": return False
            return None

        _human_decision_events[self.sid].clear() # Preparar para nueva espera
        _human_response_data[self.sid].clear()  # Limpiar datos de respuesta previos para este SID

        event_to_emit = f"human_request_{action_type}"
        self.socketio.emit(event_to_emit, details, room=self.sid)
        server_console.log(f"[WebCallbackHandler] Emitted '{event_to_emit}' to SID: {self.sid}. Waiting for response...")

        # Esperar respuesta del cliente con timeout
        if not _human_decision_events[self.sid].wait(timeout=300.0): # 5 minutos de timeout
            server_console.log(f"[WebCallbackHandler] Timeout (300s) esperando decisión humana para '{action_type}' del SID {self.sid}")
            self._cleanup_on_error_or_timeout() # Limpiar al hacer timeout
            if action_type == "confirm_rules": return False
            if action_type == "decide_action": return "plantarse", 0.0
            if action_type == "decide_steal": return False
            return None # Default general si no es un tipo conocido

        response = _human_response_data[self.sid].get(action_type)
        server_console.log(f"[WebCallbackHandler] Received response for '{action_type}' from SID {self.sid}: {response}")
        
        return response

def get_all_openrouter_models():
    try:
        import requests as http_requests 
        response = http_requests.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
        models_data = response.json()["data"]
        
        free_models = []
        for model_info in models_data:
            pricing = model_info.get("pricing", {})
            is_free = all(
                float(pricing.get(key, 0)) == 0 
                for key in ["prompt", "completion", "image", "request", 
                           "input_cache_read", "input_cache_write", 
                           "web_search", "internal_reasoning"]
            )
            
            if is_free and "text" in model_info.get("architecture", {}).get("input_modalities", []):
                free_models.append({
                    "name": model_info["name"],
                    "id": model_info["id"],
                    "context_length": model_info.get("context_length", 0),
                    "description": model_info.get("description", ""),
                    "api_url": OPENROUTER_API_URL,
                    "api_key": OPENROUTER_API_KEY,
                    "provider": "openrouter"
                })
        
        free_models.sort(key=lambda x: x["context_length"], reverse=True)
        return free_models
        
    except Exception as e:
        server_console.print(f"[red]Error fetching OpenRouter models: {e}[/red]")
        return []

def get_free_models_configs():
    predefined_models_configs = [m_config for m_config in pelusas.MODELS if m_config.get("provider") != "openrouter"]
    openrouter_models_configs = get_all_openrouter_models()
    return predefined_models_configs + openrouter_models_configs


class WebObserver(pelusas.GameObserver):
    def __init__(self, jugadores_lista, human_player_name=None):
        self.jugadores_nombres = [j.name for j in jugadores_lista]
        self.human_player_name = human_player_name

    def on_game_start(self, jugadores_lista):
        self.jugadores_nombres = [j.name for j in jugadores_lista] 
        socketio.emit('game_start', {
            'jugadores': self.jugadores_nombres,
            'mazo_count': pelusas.REPETICIONES_CARTAS * len(pelusas.CARTAS),
            'human_player_name': self.human_player_name
        })

    def on_turn_start(self, jugador_obj):
        socketio.emit('turn_start', {'jugador': jugador_obj.name})

    def on_card_drawn(self, jugador_obj, carta, zonas_temporales, zonas_personales, puntuaciones_permanentes, mazo):
        socketio.emit('card_drawn', {
            'jugador': jugador_obj.name,
            'carta': carta,
            'mazo_count': len(mazo)
        })
        # El estado completo se actualiza con update_game_state que usualmente sigue a esta acción.

    def on_decision(self, jugador_obj, decision_str):
        socketio.emit('decision', {
            'jugador': jugador_obj.name,
            'decision': decision_str
        })

    def on_turn_end(self, jugador_obj):
        socketio.emit('turn_end', {'jugador': jugador_obj.name})

    def on_game_end(self, ganador_obj, puntuaciones_permanentes_lista):
        nombres_jugadores_actuales = self.jugadores_nombres 
        socketio.emit('game_end', {
            'ganador': ganador_obj.name if ganador_obj else "Nadie",
            'puntuaciones': puntuaciones_permanentes_lista[:] if puntuaciones_permanentes_lista else [],
            'jugadores': nombres_jugadores_actuales 
        })

    def on_temporal_zone_lost(self, jugador_obj, causa="desconocida"):
        server_console.log(f"[WebObserver] Event: Temporal zone lost for {jugador_obj.name}, causa: {causa}")
        socketio.emit('temporal_zone_lost', {'jugador': jugador_obj.name, 'causa': causa})

    def on_personal_zone_lost(self, jugador_obj, causa="desconocida"):
        server_console.log(f"[WebObserver] Event: Personal zone lost for {jugador_obj.name}, causa: {causa}")
        socketio.emit('personal_zone_lost', {'jugador': jugador_obj.name, 'causa': causa})

    def on_steal_from_player(self, jugador_ladron_obj, jugador_victima_obj, carta_robada_str):
        socketio.emit('steal_from_player', {
            'jugador': jugador_ladron_obj.name,
            'victima': jugador_victima_obj.name,
            'carta': carta_robada_str
        })

    def update_game_state(self, jugadores_lista_obj, zonas_temporales, zonas_personales, puntuaciones_permanentes_lista, mazo):
        nombres_jugadores = self.jugadores_nombres
        if jugadores_lista_obj: # Si se pasa una lista actualizada de objetos jugador
             nombres_jugadores = [j.name for j in jugadores_lista_obj]
             self.jugadores_nombres = nombres_jugadores # Actualizar la lista interna del observer

        socketio.emit('update_game_state', {
            'jugadores': nombres_jugadores,
            'zonas_temporales': [list(zt) for zt in zonas_temporales],
            'zonas_personales': [list(zp) for zp in zonas_personales],
            'puntuaciones': list(puntuaciones_permanentes_lista),
            'mazo_count': len(mazo)
        })

@app.route('/')
def index_route():
    return render_template('index.html')

@app.route('/api/free_models')
def api_free_models():
    all_model_configs = get_free_models_configs()
    # Devolver solo la información necesaria para el frontend
    return jsonify([{
        "name": model["name"],
        "id": model["id"],
        "context_length": model.get("context_length", 0),
        "description": model.get("description", ""),
        "provider": model.get("provider", "unknown")
    } for model in all_model_configs])


@socketio.on('start_game')
def handle_start_game(data):
    modo_juego_tipo = data.get('modoJuego', 'single') 
    modo_seleccion_jugadores = data.get('modoSeleccion', 'random') 
    
    server_console.log(f"Recibido start_game: tipo={modo_juego_tipo}, seleccion={modo_seleccion_jugadores}, data={data}")

    jugadores_para_partida = []
    human_player_name_for_observer = None # Para pasar al WebObserver si hay un humano

    # --- Testeo y Selección de Modelos/Jugadores ---
    if modo_seleccion_jugadores == 'manual':
        selected_model_ids = data.get('selectedModels', [])
        if not selected_model_ids or len(selected_model_ids) < 1 or len(selected_model_ids) > 4:
            emit('error', {'message': 'Seleccione de 1 a 4 modelos para el modo manual.'})
            return

        all_available_model_configs = get_free_models_configs()
        # Filtrar solo los modelos que el usuario seleccionó por ID
        selected_model_configs_to_test = [m_conf for m_conf in all_available_model_configs if m_conf["id"] in selected_model_ids]
        
        if len(selected_model_configs_to_test) != len(selected_model_ids):
            # Esto podría ocurrir si un ID enviado no corresponde a un modelo conocido
            emit('error', {'message': 'Algunos modelos seleccionados manualmente no son válidos o no están disponibles.'})
            return

        operative_model_configs = test_selected_models_with_events(selected_model_configs_to_test)
        if not operative_model_configs:
            emit('error', {'message': 'Ninguno de los modelos seleccionados manualmente está operativo.'})
            return
        
        # Jugar con todos los operativos que el usuario seleccionó y pasaron el test
        jugadores_para_partida = pelusas.seleccionar_jugadores(
            num_jugadores_total=len(operative_model_configs), 
            operative_model_configs=operative_model_configs,
            modo_seleccion_ia='static', # Usar los que pasaron el test en el orden que están
            num_human_players=0
        )

    elif modo_seleccion_jugadores == 'player_vs_ia':
        # Testear todos los modelos base definidos en pelusas.MODELS
        all_operative_model_configs = test_all_models_with_events() 
        if not all_operative_model_configs:
            # Podríamos permitir humano vs humano si no hay IAs, o simplemente error.
            emit('error', {'message': 'No hay modelos de IA operativos para jugar contra ellos. Modo Jugador vs IA no puede iniciar.'})
            return

        num_ias_a_seleccionar = min(3, len(all_operative_model_configs)) # Max 3 IAs + 1 Humano = 4 total

        jugadores_para_partida = pelusas.seleccionar_jugadores(
            num_jugadores_total=num_ias_a_seleccionar + 1, # Número total de jugadores en la partida
            operative_model_configs=all_operative_model_configs, # Pasar todos los operativos para que seleccione
            modo_seleccion_ia='random', # Seleccionar aleatoriamente entre los operativos
            num_human_players=1
        )
        
        human_player_instance = next((p for p in jugadores_para_partida if p.is_human), None)
        if human_player_instance:
            # Crear y asignar el callback_handler AQUÍ
            web_callback_handler = WebCallbackHandlerForHuman(socketio, request.sid)
            human_player_instance.callback_handler = web_callback_handler # ASIGNACIÓN CRUCIAL
            human_player_name_for_observer = human_player_instance.name
            # Informar al frontend quién es el jugador humano
            socketio.emit('human_player_identity', {'name': human_player_instance.name}, room=request.sid)
        else:
            server_console.log("[Error] No se encontró instancia de HumanPlayer después de seleccionar_jugadores.")
            emit('error', {'message': 'Error crítico al configurar el jugador humano para el modo Jugador vs IA.'})
            return
            
    else: # 'random' (IAs predefinidas de pelusas.MODELS) o default
        # Testear todos los modelos base definidos en pelusas.MODELS
        all_operative_model_configs = test_all_models_with_events() 
        if not all_operative_model_configs:
            emit('error', {'message': 'No hay modelos de IA operativos disponibles para el modo aleatorio.'})
            return
        
        num_ias_a_seleccionar = min(4, len(all_operative_model_configs)) # Hasta 4 IAs
        if num_ias_a_seleccionar == 0: # Si después del test no queda ninguna IA operativa
             emit('error', {'message': 'Ninguna IA predefinida está operativa para el modo aleatorio.'})
             return

        jugadores_para_partida = pelusas.seleccionar_jugadores(
            num_jugadores_total=num_ias_a_seleccionar, 
            operative_model_configs=all_operative_model_configs,
            modo_seleccion_ia='random',
            num_human_players=0
        )

    if not jugadores_para_partida:
        emit('error', {'message': 'No se pudieron configurar jugadores para la partida.'})
        return

    # --- Iniciar Juego ---
    observer = WebObserver(jugadores_para_partida, human_player_name=human_player_name_for_observer)
    
    # Envolver la llamada a pelusas.main en una función para pasarla a start_background_task
    def game_thread_target():
        pelusas.main(
            jugadores_lista=list(jugadores_para_partida), # Pasar una copia de la lista
            partida_num=1, 
            observer=observer
        )

    if modo_juego_tipo == 'single' or modo_seleccion_jugadores == 'player_vs_ia': # Jugador vs IA es siempre single
        socketio.start_background_task(game_thread_target)
    elif modo_juego_tipo == 'hundred':
        if modo_seleccion_jugadores == 'player_vs_ia': # No tiene sentido 100 partidas con humano
             emit('error', {'message': 'El modo de 100 partidas no está soportado para Jugador vs IA.'})
             return

        def run_hundred_games():
            # Para 100 partidas, usamos la misma selección de IAs para todas.
            static_jugadores_para_cien_partidas = list(jugadores_para_partida)

            for i in range(1, 101):
                server_console.print(f"\n[bold]Iniciando partida {i} de 100...[/bold]")
                # Crear un nuevo observer para cada partida si es necesario, o reutilizar.
                # Reutilizar está bien si no guarda estado específico de la partida.
                current_observer = WebObserver(static_jugadores_para_cien_partidas, human_player_name=None) # No humano en 100 partidas

                pelusas.main(static_jugadores_para_cien_partidas, partida_num=i, observer=current_observer)
                socketio.sleep(0.5) # Pequeña pausa visual / para logs / evitar sobrecarga
            pelusas.generar_excel_resumido()
            socketio.emit("message", {"text": "Serie de 100 partidas completada. Resumen generado."})

        socketio.start_background_task(run_hundred_games)


# --- Funciones de Testeo de Modelos con Eventos ---
def test_models_core(models_to_test_configs):
    """Función núcleo para testear una lista de configuraciones de modelos."""
    operative_model_configs = []
    for i, model_config in enumerate(models_to_test_configs):
        server_console.print(f"[yellow]Testeando {model_config['name']}... (Índice {i} de {len(models_to_test_configs)})[/yellow]")
        socketio.emit('testing_model', {
            'model': model_config['name'], 
            'index': i, 
            'total': len(models_to_test_configs)
        })
        socketio.sleep(0.05) # Dar tiempo al frontend para procesar

        test_success = False
        try:
            ia_tester = pelusas.IAClient() # IAClient está en pelusas.py
            ia_tester.select_model(model_config=model_config)
            
            test_message = "Hola, ¿estás listo para jugar? Responde con {\"mensaje\": \"Entendido\"}"
            # Usar _send_api_request directamente como en pelusas.test_models
            response_json, _, success_flag = ia_tester._send_api_request(test_message)

            if success_flag: # Si la solicitud API fue exitosa (código 200, etc.)
                try:
                    if json.loads(response_json).get("mensaje") == "Entendido":
                        operative_model_configs.append(model_config)
                        server_console.print(f"[green]{model_config['name']} está operativo.[/green]")
                        test_success = True
                    else:
                        server_console.print(f"[red]{model_config['name']} no respondió 'Entendido'. Respuesta: {response_json}[/red]")
                except json.JSONDecodeError:
                    server_console.print(f"[red]{model_config['name']} devolvió JSON inválido: {response_json}[/red]")
            else: # success_flag es False
                 server_console.print(f"[red]{model_config['name']} falló la solicitud API o no fue exitosa.[/red]")
                
        except Exception as e:
            server_console.print(f"[red]Error excepcional al testear {model_config['name']}:[/red]")
            server_console.print_exception(show_locals=False) # Muestra traceback sin variables locales
        
        socketio.emit('model_tested', {
            'model': model_config['name'], 
            'index': i, 
            'success': test_success
        })
        socketio.sleep(0.05)
        time.sleep(1.0) # Pausa real entre llamadas API para no sobrecargar
    
    if not operative_model_configs:
        server_console.print("[bold red]¡Advertencia! No se encontraron modelos operativos en este testeo.[/bold red]")
    return operative_model_configs

def test_all_models_with_events():
    """Testea todos los modelos predefinidos en pelusas.MODELS."""
    # pelusas.MODELS es la lista global de modelos base.
    return test_models_core(list(pelusas.MODELS)) # Usar una copia de la lista

def test_selected_models_with_events(selected_model_configs):
    """Testea una lista específica de configuraciones de modelos."""
    return test_models_core(list(selected_model_configs)) # Usar una copia


# --- Manejadores de Respuesta Humana ---
@socketio.on('human_response_confirm_rules')
def handle_human_confirm_rules(data):
    sid = request.sid
    server_console.log(f"Recibida human_response_confirm_rules de {sid}: {data}")
    if sid in _human_decision_events and sid in _human_response_data:
        _human_response_data[sid]["confirm_rules"] = data.get('confirmed', False)
        _human_decision_events[sid].set() # Notificar al hilo del juego que la respuesta llegó
    else:
        server_console.log(f"Evento de decisión o datos no encontrados para {sid} en confirm_rules. Puede que el juego haya terminado o el SID expirado.")

@socketio.on('human_response_decide_action')
def handle_human_decide_action(data):
    sid = request.sid
    server_console.log(f"Recibida human_response_decide_action de {sid}: {data}")
    if sid in _human_decision_events and sid in _human_response_data:
        decision = data.get('decision', 'plantarse') # Default seguro
        _human_response_data[sid]["decide_action"] = (decision, 0.0) # (decision_str, tiempo_respuesta_float)
        _human_decision_events[sid].set()
    else:
        server_console.log(f"Evento de decisión o datos no encontrados para {sid} en decide_action.")

@socketio.on('human_response_decide_steal')
def handle_human_decide_steal(data):
    sid = request.sid
    server_console.log(f"Recibida human_response_decide_steal de {sid}: {data}")
    if sid in _human_decision_events and sid in _human_response_data:
        _human_response_data[sid]["decide_steal"] = data.get('steal', False) # Default seguro
        _human_decision_events[sid].set()
    else:
        server_console.log(f"Evento de decisión o datos no encontrados para {sid} en decide_steal.")

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    server_console.log(f"Cliente desconectado: {sid}. Limpiando datos de decisión humana si existen.")
    if sid in _human_decision_events:
        _human_decision_events[sid].set() # Desbloquear hilo de juego si está esperando
    # Limpiar explícitamente los datos asociados al SID
    _human_decision_events.pop(sid, None)
    _human_response_data.pop(sid, None)


if __name__ == '__main__':
    server_console.print("[bold green]Servidor Pelusas listo en http://localhost:5000[/bold green]")
    # use_reloader=False es importante si usas variables globales como _human_decision_events
    # y no quieres que se reinicien con cada guardado de archivo en modo debug.
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, use_reloader=False)