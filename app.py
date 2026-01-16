# app.py - Backend API Flask Production avec Gemini
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from datetime import datetime
import google.generativeai as genai
import os
import json
from functools import wraps
import sqlite3
import uuid
import re

app = Flask(__name__)
CORS(app)

# Configuration
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
ADMIN_SECRET = os.environ.get('ADMIN_SECRET', 'your-secure-admin-key-change-this')

# Configurer Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-exp')

# Rate Limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per hour"]
)

# Database setup
def init_db():
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    
    # Table clients
    c.execute('''CREATE TABLE IF NOT EXISTS clients
                 (client_id TEXT PRIMARY KEY,
                  company_name TEXT,
                  email TEXT,
                  config TEXT,
                  is_active INTEGER DEFAULT 1,
                  draft_mode INTEGER DEFAULT 1,
                  created_at TIMESTAMP,
                  api_calls_count INTEGER DEFAULT 0,
                  api_calls_limit INTEGER DEFAULT 500)''')
    
    # Table logs
    c.execute('''CREATE TABLE IF NOT EXISTS logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  client_id TEXT,
                  email_from TEXT,
                  email_subject TEXT,
                  email_type TEXT,
                  response_generated TEXT,
                  pdf_generated INTEGER,
                  timestamp TIMESTAMP,
                  success INTEGER)''')
    
    # Table rate_limits
    c.execute('''CREATE TABLE IF NOT EXISTS rate_limits
                 (client_id TEXT,
                  hour TEXT,
                  count INTEGER,
                  PRIMARY KEY (client_id, hour))''')
    
    conn.commit()
    conn.close()

init_db()

# Decorator pour vérifier l'authentification admin
def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or auth_header != f'Bearer {ADMIN_SECRET}':
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

# Decorator pour vérifier le client_id
def require_client(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_id = request.headers.get('X-Client-ID')
        if not client_id:
            return jsonify({'error': 'Missing client_id'}), 400
        
        # Vérifier que le client existe et est actif
        conn = sqlite3.connect('email_agent.db')
        c = conn.cursor()
        c.execute('SELECT is_active, draft_mode, api_calls_count, api_calls_limit FROM clients WHERE client_id=?', (client_id,))
        result = c.fetchone()
        conn.close()
        
        if not result:
            return jsonify({'error': 'Invalid client_id'}), 403
        
        is_active, draft_mode, calls_count, calls_limit = result
        
        if not is_active:
            return jsonify({'error': 'Client account disabled'}), 403
        
        if calls_count >= calls_limit:
            return jsonify({'error': 'API calls limit reached'}), 429
        
        # Passer les infos au contexte
        request.client_info = {
            'client_id': client_id,
            'draft_mode': draft_mode,
            'calls_count': calls_count
        }
        
        return f(*args, **kwargs)
    return decorated_function

# Fonction pour incrémenter le compteur d'appels
def increment_api_calls(client_id):
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    c.execute('UPDATE clients SET api_calls_count = api_calls_count + 1 WHERE client_id=?', (client_id,))
    conn.commit()
    conn.close()

# Fonction pour logger
def log_action(client_id, email_from, email_subject, email_type, response, pdf_generated, success):
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    c.execute('''INSERT INTO logs (client_id, email_from, email_subject, email_type, 
                 response_generated, pdf_generated, timestamp, success)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (client_id, email_from, email_subject, email_type, response[:500] if response else '', 
               pdf_generated, datetime.now(), success))
    conn.commit()
    conn.close()

# ADMIN ENDPOINTS

@app.route('/admin/clients', methods=['GET'])
@require_admin
def get_clients():
    """Liste tous les clients"""
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    c.execute('SELECT client_id, company_name, email, is_active, draft_mode, api_calls_count, api_calls_limit, created_at FROM clients')
    clients = []
    for row in c.fetchall():
        clients.append({
            'client_id': row[0],
            'company_name': row[1],
            'email': row[2],
            'is_active': bool(row[3]),
            'draft_mode': bool(row[4]),
            'api_calls_count': row[5],
            'api_calls_limit': row[6],
            'created_at': row[7]
        })
    conn.close()
    return jsonify(clients)

@app.route('/admin/clients', methods=['POST'])
@require_admin
def create_client():
    """Créer un nouveau client"""
    data = request.json
    client_id = str(uuid.uuid4())
    
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    c.execute('''INSERT INTO clients (client_id, company_name, email, config, created_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (client_id, data['company_name'], data['email'], 
               json.dumps(data.get('config', {})), datetime.now()))
    conn.commit()
    conn.close()
    
    return jsonify({'client_id': client_id, 'message': 'Client created successfully'}), 201

@app.route('/admin/clients/<client_id>/toggle', methods=['POST'])
@require_admin
def toggle_client(client_id):
    """Activer/désactiver un client"""
    data = request.json
    field = data.get('field', 'is_active')  # is_active ou draft_mode
    value = data.get('value', True)
    
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    c.execute(f'UPDATE clients SET {field}=? WHERE client_id=?', (int(value), client_id))
    conn.commit()
    conn.close()
    
    return jsonify({'message': f'{field} updated successfully'})

@app.route('/admin/clients/<client_id>/config', methods=['PUT'])
@require_admin
def update_client_config(client_id):
    """Mettre à jour la configuration d'un client"""
    config = request.json
    
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    c.execute('UPDATE clients SET config=? WHERE client_id=?', 
              (json.dumps(config), client_id))
    conn.commit()
    conn.close()
    
    return jsonify({'message': 'Configuration updated successfully'})

@app.route('/admin/logs', methods=['GET'])
@require_admin
def get_logs():
    """Récupérer tous les logs"""
    client_id = request.args.get('client_id')
    limit = int(request.args.get('limit', 100))
    
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    
    if client_id:
        c.execute('''SELECT * FROM logs WHERE client_id=? 
                     ORDER BY timestamp DESC LIMIT ?''', (client_id, limit))
    else:
        c.execute('SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?', (limit,))
    
    logs = []
    for row in c.fetchall():
        logs.append({
            'id': row[0],
            'client_id': row[1],
            'email_from': row[2],
            'email_subject': row[3],
            'email_type': row[4],
            'response_generated': row[5],
            'pdf_generated': bool(row[6]),
            'timestamp': row[7],
            'success': bool(row[8])
        })
    conn.close()
    
    return jsonify(logs)

@app.route('/admin/stats', methods=['GET'])
@require_admin
def get_stats():
    """Statistiques globales"""
    conn = sqlite3.connect('email_agent.db')
    c = conn.cursor()
    
    # Total clients
    c.execute('SELECT COUNT(*) FROM clients')
    total_clients = c.fetchone()[0]
    
    # Clients actifs
    c.execute('SELECT COUNT(*) FROM clients WHERE is_active=1')
    active_clients = c.fetchone()[0]
    
    # Total appels API
    c.execute('SELECT SUM(api_calls_count) FROM clients')
    total_api_calls = c.fetchone()[0] or 0
    
    # Emails traités aujourd'hui
    c.execute('SELECT COUNT(*) FROM logs WHERE DATE(timestamp) = DATE("now")')
    emails_today = c.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'total_clients': total_clients,
        'active_clients': active_clients,
        'total_api_calls': total_api_calls,
        'emails_today': emails_today
    })

# CLIENT ENDPOINTS

@app.route('/api/analyze-email', methods=['POST'])
@require_client
@limiter.limit("50 per hour")
def analyze_email():
    """Analyser un email et générer une réponse avec Gemini"""
    client_id = request.client_info['client_id']
    draft_mode = request.client_info['draft_mode']
    
    data = request.json
    email_from = data.get('from')
    email_subject = data.get('subject')
    email_body = data.get('body')
    
    if not all([email_from, email_subject, email_body]):
        return jsonify({'error': 'Missing email data'}), 400
    
    try:
        # Récupérer la config du client
        conn = sqlite3.connect('email_agent.db')
        c = conn.cursor()
        c.execute('SELECT config FROM clients WHERE client_id=?', (client_id,))
        config = json.loads(c.fetchone()[0])
        conn.close()
        
        # Analyser le type d'email avec Gemini
        analysis_prompt = f"""Analyse cet email et détermine son type parmi :
- DEVIS : demande de devis/proposition commerciale
- RELANCE_PAIEMENT : facture impayée ou relance
- INFORMATION : demande de renseignement
- RECLAMATION : plainte ou problème
- AUTRE : autre type

Email:
De: {email_from}
Objet: {email_subject}
Corps: {email_body}

Réponds UNIQUEMENT par le type en MAJUSCULES, sans aucun autre texte."""

        analysis_response = model.generate_content(analysis_prompt)
        email_type = analysis_response.text.strip()
        
        # Générer la réponse appropriée
        system_prompt = build_system_prompt(email_type, config)
        
        generation_prompt = f"""{system_prompt}

Email reçu:
De: {email_from}
Objet: {email_subject}

{email_body}

{f'Génère l\'email d\'accompagnement puis les données du devis en JSON, séparés par ---SEPARATION---' if email_type == 'DEVIS' else 'Génère uniquement l\'email de réponse professionnelle.'}"""

        response = model.generate_content(generation_prompt)
        full_response = response.text
        
        # Traiter la réponse
        result = {
            'email_type': email_type,
            'draft_mode': draft_mode,
            'email_response': '',
            'devis_data': None
        }
        
        if email_type == 'DEVIS':
            parts = full_response.split('---SEPARATION---')
            result['email_response'] = parts[0].strip()
            
            # Extraire le JSON du devis
            try:
                json_match = re.search(r'\{[\s\S]*\}', parts[1] if len(parts) > 1 else '')
                if json_match:
                    devis_json = json_match.group(0)
                    # Nettoyer le JSON (enlever les backticks markdown si présents)
                    devis_json = re.sub(r'```json\s*|\s*```', '', devis_json)
                    result['devis_data'] = json.loads(devis_json)
            except Exception as e:
                print(f"Erreur parsing devis: {e}")
                pass
        else:
            result['email_response'] = full_response
        
        # Logger l'action
        increment_api_calls(client_id)
        log_action(client_id, email_from, email_subject, email_type, 
                   result['email_response'], bool(result['devis_data']), True)
        
        return jsonify(result)
        
    except Exception as e:
        log_action(client_id, email_from, email_subject, '', str(e), False, False)
        return jsonify({'error': str(e)}), 500

def build_system_prompt(email_type, config):
    """Construire le prompt système selon le type d'email"""
    
    company_name = config.get('companyName', config.get('company_name', 'Entreprise'))
    signatory_name = config.get('signatoryName', config.get('signatory_name', 'Le Directeur'))
    signatory_role = config.get('signatoryRole', config.get('signatory_role', 'Directeur'))
    company_desc = config.get('companyDescription', config.get('company_description', ''))
    email = config.get('email', 'contact@entreprise.fr')
    phone = config.get('phone', '')
    address = config.get('address', '')
    siret = config.get('siret', '')
    tva_number = config.get('tvaNumber', config.get('tva_number', ''))
    payment_delay = config.get('paymentDelay', config.get('payment_delay', '30'))
    bank_details = config.get('bankDetails', config.get('bank_details', ''))
    
    if email_type == 'DEVIS':
        return f"""Tu es l'assistant professionnel de {signatory_name}, {signatory_role} de {company_name}.
{company_desc}

INFORMATIONS ENTREPRISE:
- Nom: {company_name}
- Email: {email}
- Téléphone: {phone}
- Adresse: {address}
- SIRET: {siret}
- TVA: {tva_number}
- Paiement: {payment_delay} jours
- RIB: {bank_details}

INSTRUCTIONS POUR LE DEVIS:
1. Analyser les besoins du client avec attention
2. Proposer des prestations détaillées et professionnelles (3-5 lignes minimum)
3. Donner des prix réalistes et cohérents en euros
4. Structure du devis: Description | Quantité | Prix unitaire HT | Total HT
5. Calculer correctement la TVA à 20%
6. Proposer un délai de réalisation réaliste
7. Inclure les conditions de paiement

INSTRUCTIONS POUR L'EMAIL:
1. Email d'accompagnement professionnel, chaleureux et personnalisé
2. Remercier sincèrement pour la demande
3. Mentionner que le devis détaillé suit
4. Proposer un appel téléphonique pour discuter des détails
5. Rester disponible pour toute question
6. Signer avec nom et fonction

FORMAT DE RÉPONSE:
Génère DEUX choses séparées par ---SEPARATION--- :

1. D'abord l'EMAIL d'accompagnement (format texte normal)

2. Ensuite les DONNÉES DU DEVIS en JSON strict (sans markdown, sans backticks):
{{
  "devisNumber": "DEVIS-2025-{datetime.now().strftime('%m%d%H%M')}",
  "date": "{datetime.now().strftime('%d/%m/%Y')}",
  "clientName": "Nom du client extrait de l'email",
  "clientAddress": "Adresse si mentionnée, sinon 'Non communiquée'",
  "items": [
    {{"description": "Description détaillée de la prestation 1", "quantity": 1, "unitPrice": 1000, "total": 1000}},
    {{"description": "Description détaillée de la prestation 2", "quantity": 2, "unitPrice": 500, "total": 1000}}
  ],
  "subtotal": 2000,
  "tva": 400,
  "total": 2400,
  "validityDays": 30,
  "deliveryTime": "X semaines",
  "paymentTerms": "{payment_delay} jours après signature"
}}

IMPORTANT: Les calculs doivent être exacts (subtotal = somme des totaux, tva = subtotal * 0.2, total = subtotal + tva)"""
    
    elif email_type == 'RELANCE_PAIEMENT':
        return f"""Tu es l'assistant de {signatory_name}, {signatory_role} de {company_name}.

RÔLE: Gérer une demande liée à un paiement avec professionnalisme et fermeté bienveillante.

INSTRUCTIONS:
1. Analyser la situation (retard de paiement, demande d'échelonnement, difficulté financière)
2. Rester courtois mais ferme en cas de retard
3. Si demande d'échelonnement: proposer une solution raisonnable (ex: 3 mensualités maximum)
4. Rappeler les coordonnées bancaires: {bank_details}
5. Fixer une deadline claire et précise
6. Mentionner les conséquences en cas de non-paiement (intérêts de retard, etc.)
7. Ton professionnel mais compréhensif des difficultés

Génère uniquement l'EMAIL de réponse."""
    
    elif email_type == 'RECLAMATION':
        return f"""Tu es l'assistant de {signatory_name}, {signatory_role} de {company_name}.

RÔLE: Gérer une réclamation avec empathie maximale et professionnalisme.

INSTRUCTIONS:
1. S'excuser SINCÈREMENT et sans réserve pour le désagrément causé
2. Reconnaître le problème de manière claire et directe
3. Proposer une solution IMMÉDIATE et CONCRÈTE
4. Offrir un geste commercial approprié et généreux (remise, remplacement gratuit, compensation)
5. Rassurer sur le suivi personnalisé et prioritaire
6. Proposer un appel téléphonique URGENT: {phone}
7. Montrer que la satisfaction du client est la priorité absolue
8. Ton: empathique, rassurant, orienté solution, humble

Génère uniquement l'EMAIL de réponse."""
    
    elif email_type == 'INFORMATION':
        return f"""Tu es l'assistant de {signatory_name}, {signatory_role} de {company_name}.
{company_desc}

RÔLE: Répondre à une demande d'information de manière claire, complète et engageante.

INSTRUCTIONS:
1. Remercier chaleureusement pour l'intérêt porté à {company_name}
2. Répondre PRÉCISÉMENT et COMPLÈTEMENT à toutes les questions posées
3. Apporter des détails supplémentaires utiles et pertinents
4. Proposer des ressources complémentaires si approprié
5. Inviter à un échange téléphonique pour approfondir: {phone}
6. Suggérer une prochaine étape concrète (rendez-vous, démonstration, etc.)
7. Ton chaleureux, serviable et expert

Génère uniquement l'EMAIL de réponse."""
    
    else:
        return f"""Tu es l'assistant professionnel de {signatory_name}, {signatory_role} de {company_name}.

RÔLE: Répondre de manière appropriée, personnalisée et professionnelle à toute demande.

INSTRUCTIONS:
1. Analyser le contexte et l'intention de l'email
2. Répondre de manière claire et structurée
3. Adopter un ton adapté à la situation
4. Proposer une suite concrète si pertinent
5. Rester toujours professionnel et courtois

Génère uniquement l'EMAIL de réponse."""

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok', 
        'timestamp': datetime.now().isoformat(),
        'ai_provider': 'Google Gemini 2.0 Flash'
    })

@app.route('/', methods=['GET'])
def index():
    """Page d'accueil de l'API"""
    return jsonify({
        'service': 'Email Agent API',
        'version': '2.0',
        'ai_provider': 'Google Gemini 2.0 Flash',
        'endpoints': {
            'admin': [
                'GET /admin/clients',
                'POST /admin/clients',
                'POST /admin/clients/<id>/toggle',
                'PUT /admin/clients/<id>/config',
                'GET /admin/logs',
                'GET /admin/stats'
            ],
            'client': [
                'POST /api/analyze-email'
            ]
        },
        'documentation': 'Contact admin for API access'
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))