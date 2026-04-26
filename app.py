from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from models import db, Cliente, Pagamento, ContratoHistorico
from datetime import date, datetime
from functools import wraps
import os, base64, re, time

app = Flask(__name__, template_folder='templates')
_db_url = os.environ.get('DATABASE_URL', '')
if not _db_url:
    _db_url = 'sqlite:///fincontrol.db'
elif _db_url.startswith('postgres://'):
    _db_url = 'postgresql://' + _db_url[len('postgres://'):]
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# ── Segurança: força SECRET_KEY forte no ambiente ────────────────
if not app.config['SECRET_KEY']:
    raise RuntimeError("SECRET_KEY não configurada! Defina a variável de ambiente SECRET_KEY.")

# ── PINs via variável de ambiente APENAS ────────────────────────
PINS = {
    'owner':       os.environ.get('PIN_OWNER', ''),
    'funcionario': os.environ.get('PIN_FUNC',  ''),
}
if not PINS['owner'] or not PINS['funcionario']:
    raise RuntimeError("PIN_OWNER e PIN_FUNC devem ser definidos como variáveis de ambiente!")

# ── API Key do bot via variável de ambiente APENAS ───────────────
BOT_API_KEY = os.environ.get('BOT_API_KEY', '')
if not BOT_API_KEY:
    raise RuntimeError("BOT_API_KEY não configurada!")

# ── Rate limit simples para login (evita força bruta) ───────────
_login_attempts = {}  # ip -> (tentativas, timestamp_primeiro)
MAX_TENTATIVAS  = 5
JANELA_SEGUNDOS = 300  # 5 minutos

def check_rate_limit(ip: str) -> bool:
    """Retorna True se o IP está bloqueado."""
    now = time.time()
    if ip in _login_attempts:
        tentativas, primeiro = _login_attempts[ip]
        if now - primeiro > JANELA_SEGUNDOS:
            # Janela expirou, reseta
            _login_attempts[ip] = (1, now)
            return False
        if tentativas >= MAX_TENTATIVAS:
            return True
        _login_attempts[ip] = (tentativas + 1, primeiro)
    else:
        _login_attempts[ip] = (1, now)
    return False

def reset_rate_limit(ip: str):
    _login_attempts.pop(ip, None)

db.init_app(app)

with app.app_context():
    db.create_all()

# ── Decorators de autenticação ───────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'role' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'owner':
            flash('Apenas o owner pode executar esta ação.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Aceita APENAS via header — nunca via URL (evita logs com key exposta)
        key = request.headers.get('X-API-Key')
        if not key or key != BOT_API_KEY:
            return jsonify(erro="não autorizado"), 403
        return f(*args, **kwargs)
    return decorated

# ── Helpers ──────────────────────────────────────────────────────

def today():
    return date.today().isoformat()

def this_month():
    return date.today().strftime('%Y-%m')

def salvar_arquivo(file):
    if file and file.filename:
        data = file.read()
        b64 = base64.b64encode(data).decode('utf-8')
        mime = file.content_type or 'application/octet-stream'
        return f"data:{mime};base64,{b64}"
    return None

# ── Rotas web ────────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'role' in session:
        return redirect(url_for('dashboard'))

    ip = request.remote_addr
    error = None

    if request.method == 'POST':
        # Verifica rate limit antes de checar PIN
        if check_rate_limit(ip):
            minutos = JANELA_SEGUNDOS // 60
            error = f'Muitas tentativas. Aguarde {minutos} minutos.'
            return render_template('login.html', error=error)

        role = request.form.get('role')
        pin  = request.form.get('pin', '')

        if role in PINS and pin == PINS[role]:
            reset_rate_limit(ip)
            session['role'] = role
            session.permanent = False  # sessão expira ao fechar o browser
            return redirect(url_for('dashboard'))

        error = 'PIN incorreto. Tente novamente.'

    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    clientes = Cliente.query.filter_by(ativo=True).all()
    pags_hoje = Pagamento.query.filter_by(data=today()).all()
    total_hoje = sum(p.valor for p in pags_hoje)
    mes = this_month()
    pags_mes = Pagamento.query.filter(Pagamento.data.startswith(mes)).all()
    total_mes = sum(p.valor for p in pags_mes)
    ativos     = [c for c in clientes if c.diarias_pagas < 20]
    aguardando = [c for c in clientes if c.diarias_pagas >= 20]
    em_atraso  = [c for c in ativos if c.dias_em_atraso > 0]
    return render_template('dashboard.html',
        clientes=clientes,
        ativos=len(ativos),
        aguardando=len(aguardando),
        em_atraso=len(em_atraso),
        total_hoje=total_hoje,
        total_mes=total_mes,
        role=session['role'],
        hoje=today()
    )

@app.route('/clientes')
@login_required
def clientes():
    filtro = request.args.get('f', 'todos')
    todos = Cliente.query.order_by(Cliente.criado_em.desc()).all()
    if filtro == 'ativos':
        lista = [c for c in todos if c.ativo and c.diarias_pagas < 20]
    elif filtro == 'aguardando':
        lista = [c for c in todos if c.ativo and c.diarias_pagas >= 20]
    elif filtro == 'arquivados':
        lista = [c for c in todos if not c.ativo]
    elif filtro == 'atraso':
        lista = [c for c in todos if c.ativo and c.dias_em_atraso > 0]
    else:
        lista = todos
    return render_template('clientes.html', clientes=lista, filtro=filtro, role=session['role'])

@app.route('/cadastrar', methods=['GET', 'POST'])
@login_required
def cadastrar():
    if request.method == 'POST':
        nome  = request.form['nome'].strip()
        valor = float(request.form['valor_diaria'])
        dt    = request.form.get('data_inicio') or today()
        if not nome or valor <= 0:
            flash('Preencha todos os campos corretamente.', 'error')
            return redirect(url_for('cadastrar'))
        foto_data    = salvar_arquivo(request.files.get('foto'))
        arquivo_data = salvar_arquivo(request.files.get('arquivo'))
        c = Cliente(
            nome=nome,
            whatsapp=request.form.get('whatsapp', '').strip(),
            cpf=request.form.get('cpf', '').strip(),
            limite=float(request.form.get('limite') or 0),
            endereco=request.form.get('endereco', '').strip(),
            email=request.form.get('email', '').strip(),
            chave_pix=request.form.get('chave_pix', '').strip(),
            valor_diaria=valor,
            data_inicio=dt,
            foto_url=foto_data,
            arquivo_url=arquivo_data
        )
        db.session.add(c)
        db.session.commit()
        flash(f'Cliente {nome} cadastrado com sucesso!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('cadastrar.html', hoje=today())

@app.route('/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar(id):
    c = Cliente.query.get_or_404(id)
    if request.method == 'POST':
        c.nome         = request.form['nome'].strip()
        c.whatsapp     = request.form.get('whatsapp', '').strip()
        c.cpf          = request.form.get('cpf', '').strip()
        c.limite       = float(request.form.get('limite') or 0)
        c.endereco     = request.form.get('endereco', '').strip()
        c.email        = request.form.get('email', '').strip()
        c.chave_pix    = request.form.get('chave_pix', '').strip()
        c.valor_diaria = float(request.form['valor_diaria'])
        c.data_inicio  = request.form.get('data_inicio') or c.data_inicio
        foto = request.files.get('foto')
        if foto and foto.filename:
            c.foto_url = salvar_arquivo(foto)
        arquivo = request.files.get('arquivo')
        if arquivo and arquivo.filename:
            c.arquivo_url = salvar_arquivo(arquivo)
        db.session.commit()
        flash('Cliente atualizado!', 'success')
        return redirect(url_for('dashboard'))
    pags = Pagamento.query.filter_by(cliente_id=id).order_by(Pagamento.data.desc()).all()
    hist = ContratoHistorico.query.filter_by(cliente_id=id).order_by(ContratoHistorico.data_fim.desc()).all()
    return render_template('editar.html', c=c, pags=pags, hist=hist, role=session['role'])

@app.route('/apagar/<int:id>', methods=['POST'])
@login_required
@owner_required
def apagar(id):
    c = Cliente.query.get_or_404(id)
    nome = c.nome
    db.session.delete(c)
    db.session.commit()
    flash(f'Cliente {nome} apagado.', 'warn')
    return redirect(url_for('dashboard'))

@app.route('/pagar/<int:id>', methods=['POST'])
@login_required
def pagar(id):
    c     = Cliente.query.get_or_404(id)
    valor = float(request.form['valor'])
    obs   = request.form.get('obs', '').strip()
    if valor <= 0:
        flash('Valor inválido.', 'error')
        return redirect(url_for('dashboard'))
    saldo = c.saldo_pendente + valor
    diarias_novas = int(saldo // c.valor_diaria)
    c.saldo_pendente = round(saldo % c.valor_diaria, 2)
    diarias_novas = min(diarias_novas, 20 - c.diarias_pagas)
    c.diarias_pagas = min(20, c.diarias_pagas + diarias_novas)
    p = Pagamento(cliente_id=id, data=today(), valor=valor, diarias=diarias_novas, obs=obs)
    db.session.add(p)
    db.session.commit()
    if c.diarias_pagas >= 20:
        flash(f'{c.nome} completou as 20 diárias!', 'success')
    elif diarias_novas == 0:
        flash(f'R$ {valor:.2f} registrado. Faltam R$ {c.valor_diaria - c.saldo_pendente:.2f} para próxima diária.', 'info')
    else:
        flash(f'+{diarias_novas} diária(s) para {c.nome}. Total: {c.diarias_pagas}/20.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/desfazer/<int:pag_id>', methods=['POST'])
@login_required
@owner_required
def desfazer(pag_id):
    p  = Pagamento.query.get_or_404(pag_id)
    c  = p.cliente
    c.diarias_pagas = max(0, c.diarias_pagas - p.diarias)
    c.saldo_pendente = max(0.0, c.saldo_pendente - (p.valor - p.diarias * c.valor_diaria))
    cid = c.id
    db.session.delete(p)
    db.session.commit()
    flash('Pagamento desfeito.', 'warn')
    return redirect(url_for('editar', id=cid))

@app.route('/renovar/<int:id>', methods=['POST'])
@login_required
def renovar(id):
    c = Cliente.query.get_or_404(id)
    novo_valor = float(request.form['valor_diaria'])
    nova_data  = request.form.get('data_inicio') or today()
    hist = ContratoHistorico(
        cliente_id=id, data_inicio=c.data_inicio, data_fim=today(),
        valor_diaria=c.valor_diaria, total_pago=c.total_pago
    )
    db.session.add(hist)
    c.diarias_pagas  = 0
    c.saldo_pendente = 0.0
    c.valor_diaria   = novo_valor
    c.data_inicio    = nova_data
    c.ativo          = True
    db.session.commit()
    flash(f'Contrato de {c.nome} renovado!', 'success')
    return redirect(url_for('dashboard'))

# ── API para o Bot ───────────────────────────────────────────────

@app.route('/api/inadimplentes')
@api_key_required
def api_inadimplentes():
    clientes = Cliente.query.filter_by(ativo=True).all()
    lista = []
    for c in clientes:
        if c.dias_em_atraso > 0:
            lista.append({
                'id':            c.id,
                'nome':          c.nome,
                'whatsapp':      c.whatsapp,
                'dias_atraso':   c.dias_em_atraso,
                'valor_atraso':  c.valor_em_atraso,
                'diarias_pagas': c.diarias_pagas
            })
    return jsonify(lista)

@app.route('/api/stats')
@api_key_required
def api_stats():
    mes        = this_month()
    pags_mes   = Pagamento.query.filter(Pagamento.data.startswith(mes)).all()
    total_mes  = sum(p.valor for p in pags_mes)
    pags_hoje  = Pagamento.query.filter_by(data=today()).all()
    total_hoje = sum(p.valor for p in pags_hoje)
    ativos     = Cliente.query.filter_by(ativo=True).all()
    em_atraso  = len([c for c in ativos if c.dias_em_atraso > 0])
    return jsonify(total_mes=total_mes, total_hoje=total_hoje, em_atraso=em_atraso)

@app.route('/api/cliente_por_whatsapp/<numero>')
@api_key_required
def api_cliente_por_whatsapp(numero):
    numero_limpo = re.sub(r'\D', '', numero)
    if numero_limpo.startswith('55') and len(numero_limpo) > 11:
        numero_limpo = numero_limpo[2:]
    clientes = Cliente.query.filter_by(ativo=True).all()
    for c in clientes:
        if c.whatsapp:
            wa = re.sub(r'\D', '', c.whatsapp)
            if len(wa) >= 8 and len(numero_limpo) >= 8 and wa[-8:] == numero_limpo[-8:]:
                return jsonify({
                    'id':              c.id,
                    'nome':            c.nome,
                    'whatsapp':        c.whatsapp,
                    'diarias_pagas':   c.diarias_pagas,
                    'total_pago':      c.total_pago,
                    'dias_em_atraso':  c.dias_em_atraso,
                    'valor_em_atraso': c.valor_em_atraso
                })
    return jsonify(None), 404

@app.route('/api/pagar/<int:id>', methods=['POST'])
@api_key_required
def api_pagar(id):
    c     = Cliente.query.get_or_404(id)
    data  = request.json or {}
    valor = float(data.get('valor', 0))
    obs   = data.get('obs', 'Pago via bot WhatsApp')
    if valor <= 0:
        return jsonify(erro='Valor inválido'), 400
    saldo            = c.saldo_pendente + valor
    diarias_novas    = int(saldo // c.valor_diaria)
    c.saldo_pendente = round(saldo % c.valor_diaria, 2)
    diarias_novas    = min(diarias_novas, 20 - c.diarias_pagas)
    c.diarias_pagas  = min(20, c.diarias_pagas + diarias_novas)
    p = Pagamento(cliente_id=id, data=today(), valor=valor, diarias=diarias_novas, obs=obs)
    db.session.add(p)
    db.session.commit()
    return jsonify(ok=True, diarias_pagas=c.diarias_pagas, diarias_novas=diarias_novas)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False)
