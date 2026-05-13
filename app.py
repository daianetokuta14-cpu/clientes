from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from models import db, Tenant, Cliente, Pagamento, ContratoHistorico, Parcela
from datetime import date, datetime
from functools import wraps
import os, base64, re, time

app = Flask(__name__, template_folder='templates', static_folder='static')

_db_url = os.environ.get('DATABASE_URL', '')
if not _db_url:
    _db_url = 'sqlite:///megacredito.db'
elif _db_url.startswith('postgres://'):
    _db_url = 'postgresql://' + _db_url[len('postgres://'):]

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

if not app.config['SECRET_KEY']:
    raise RuntimeError("SECRET_KEY não configurada!")

# PINs continuam existindo — são por tenant (configurados no Railway)
# Na versão SaaS cada tenant tem seus próprios PINs salvos na sessão
# Para simplicidade, o owner do SaaS define PINs globais como fallback,
# mas o correto é cada tenant ter seus próprios armazenados na tabela ou sessão.
# Aqui mantemos compatibilidade: PIN_OWNER e PIN_FUNC continuam funcionando.
PINS = {
    'owner':       os.environ.get('PIN_OWNER', ''),
    'funcionario': os.environ.get('PIN_FUNC',  ''),
}

BOT_API_KEY = os.environ.get('BOT_API_KEY', '')
ADMIN_KEY   = os.environ.get('ADMIN_KEY', '')    # sua chave secreta de admin
WPP_SUPORTE = os.environ.get('WPP_SUPORTE', '')  # seu número padrão de suporte

_login_attempts = {}
MAX_TENTATIVAS  = 5
JANELA_SEGUNDOS = 300

def check_rate_limit(ip):
    now = time.time()
    if ip in _login_attempts:
        t, primeiro = _login_attempts[ip]
        if now - primeiro > JANELA_SEGUNDOS:
            _login_attempts[ip] = (1, now); return False
        if t >= MAX_TENTATIVAS: return True
        _login_attempts[ip] = (t + 1, primeiro)
    else:
        _login_attempts[ip] = (1, now)
    return False

def reset_rate_limit(ip):
    _login_attempts.pop(ip, None)

db.init_app(app)

with app.app_context():
    db.create_all()
    import secrets as _sec

    def _run(sql):
        try:
            with db.engine.connect() as c:
                c.execute(db.text(sql)); c.commit()
        except Exception as e:
            print(f"[MIGRATION] {sql[:80]} — {e}")

    # Migrations para banco existente (adiciona colunas sem quebrar)
    _run("ALTER TABLE pagamento ADD COLUMN IF NOT EXISTS hash_arquivo VARCHAR(64) DEFAULT ''")
    _run("ALTER TABLE pagamento ADD COLUMN IF NOT EXISTS codigo_tx VARCHAR(100) DEFAULT ''")
    _run("ALTER TABLE pagamento ADD COLUMN IF NOT EXISTS parcela_id INTEGER REFERENCES parcela(id)")
    _run("ALTER TABLE cliente ALTER COLUMN foto_url TYPE TEXT")
    _run("ALTER TABLE cliente ALTER COLUMN arquivo_url TYPE TEXT")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS token_link VARCHAR(48)")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS tipo_cobranca VARCHAR(20) DEFAULT 'diaria'")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS total_diarias INTEGER DEFAULT 20")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS valor_mensalidade FLOAT DEFAULT 0")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS dia_vencimento INTEGER DEFAULT 10")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS cobranca_recorrente BOOLEAN DEFAULT TRUE")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS juros_atraso FLOAT DEFAULT 0")
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS obs_contrato TEXT DEFAULT ''")
    # Novas colunas multi-tenant
    _run("ALTER TABLE cliente ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES tenant(id)")
    _run("ALTER TABLE pagamento ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES tenant(id)")
    _run("ALTER TABLE parcela ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES tenant(id)")
    _run("ALTER TABLE contrato_historico ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES tenant(id)")

    # Gera token para clientes sem token
    try:
        with db.engine.connect() as c:
            rows = c.execute(db.text("SELECT id FROM cliente WHERE token_link IS NULL OR token_link = ''")).fetchall()
            for row in rows:
                c.execute(db.text("UPDATE cliente SET token_link = :tk WHERE id = :id"),
                          {"tk": _sec.token_urlsafe(32), "id": row[0]})
            c.commit()
    except Exception as e:
        print(f"[MIGRATION] token_link — {e}")


# ── Helpers ────────────────────────────────────────────────────
def _now_manaus():
    from datetime import timezone, timedelta
    return datetime.now(tz=timezone(timedelta(hours=-4)))

def today():
    return _now_manaus().date().isoformat()

def this_month():
    return _now_manaus().strftime('%Y-%m')

def salvar_arquivo(file):
    if file and file.filename:
        data = file.read()
        b64  = base64.b64encode(data).decode('utf-8')
        mime = file.content_type or 'application/octet-stream'
        return f"data:{mime};base64,{b64}"
    return None

def gerar_parcela_mes(cliente):
    if cliente.tipo_cobranca != 'mensalidade': return None
    hoje = date.today()
    competencia = f"{hoje.year}-{hoje.month:02d}"
    existente = Parcela.query.filter_by(cliente_id=cliente.id, competencia=competencia).first()
    if existente: return existente
    dia = min(cliente.dia_vencimento, 28)
    venc = date(hoje.year, hoje.month, dia).isoformat()
    p = Parcela(tenant_id=cliente.tenant_id, cliente_id=cliente.id,
                competencia=competencia, vencimento=venc, valor=cliente.valor_mensalidade)
    db.session.add(p); db.session.commit()
    return p

def get_tenant():
    """Retorna o Tenant logado ou None."""
    tid = session.get('tenant_id')
    if not tid: return None
    return Tenant.query.get(tid)

def tid():
    """Atalho para pegar tenant_id da sessão."""
    return session.get('tenant_id')


# ── Decorators ─────────────────────────────────────────────────
def tenant_required(f):
    """Garante que o tenant está logado via e-mail/senha e ativo."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'tenant_id' not in session:
            return redirect(url_for('login_app'))
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    """Garante que o PIN foi digitado (role na sessão)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'tenant_id' not in session:
            return redirect(url_for('login_app'))
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
        key = request.headers.get('X-API-Key')
        if not key or key != BOT_API_KEY:
            return jsonify(erro="não autorizado"), 403
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.args.get('key') or request.form.get('key') or request.headers.get('X-Admin-Key', '')
        if not ADMIN_KEY or key != ADMIN_KEY:
            return 'Acesso negado', 403
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════
# AUTENTICAÇÃO — ETAPA 1: login por e-mail/senha (tenant)
# ══════════════════════════════════════════════════════════════

@app.route('/acesso', methods=['GET', 'POST'])
def login_app():
    """Primeira tela: e-mail + senha do tenant."""
    if 'tenant_id' in session:
        if 'role' in session:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    ip = request.remote_addr
    error = None

    if request.method == 'POST':
        if check_rate_limit(ip):
            error = f'Muitas tentativas. Aguarde {JANELA_SEGUNDOS // 60} minutos.'
            return render_template('login_app.html', error=error)

        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')

        tenant = Tenant.query.filter_by(email=email).first()

        if not tenant or not tenant.verificar_senha(senha):
            error = 'E-mail ou senha incorretos.'
            return render_template('login_app.html', error=error)

        reset_rate_limit(ip)

        if tenant.status == 'pausado':
            wpp = tenant.wpp_suporte or WPP_SUPORTE
            return render_template('app_pausado.html', wpp=wpp)

        session['tenant_id'] = tenant.id
        session['tenant_nome'] = tenant.nome
        session.permanent = False
        return redirect(url_for('login'))

    return render_template('login_app.html', error=error)


# ══════════════════════════════════════════════════════════════
# AUTENTICAÇÃO — ETAPA 2: PIN (owner / funcionário)
# ══════════════════════════════════════════════════════════════

@app.route('/', methods=['GET', 'POST'])
def login():
    """Segunda tela: PIN de owner ou funcionário."""
    if 'tenant_id' not in session:
        return redirect(url_for('login_app'))
    if 'role' in session:
        return redirect(url_for('dashboard'))

    ip = request.remote_addr
    error = None

    if request.method == 'POST':
        if check_rate_limit(ip):
            error = f'Muitas tentativas. Aguarde {JANELA_SEGUNDOS // 60} minutos.'
            return render_template('login.html', error=error)
        role = request.form.get('role')
        pin  = request.form.get('pin', '')
        if role in PINS and PINS[role] and pin == PINS[role]:
            reset_rate_limit(ip)
            session['role'] = role
            return redirect(url_for('dashboard'))
        error = 'PIN incorreto. Tente novamente.'

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_app'))


@app.route('/healthz')
def healthz():
    return 'ok', 200


# ══════════════════════════════════════════════════════════════
# ROTAS PRINCIPAIS (todas filtradas por tenant_id)
# ══════════════════════════════════════════════════════════════

@app.route('/dashboard')
@login_required
def dashboard():
    clientes = Cliente.query.filter_by(tenant_id=tid(), ativo=True).all()
    for c in clientes:
        if c.tipo_cobranca == 'mensalidade':
            gerar_parcela_mes(c)

    pags_hoje  = Pagamento.query.filter_by(tenant_id=tid(), data=today()).all()
    total_hoje = sum(p.valor for p in pags_hoje if p.valor > 0)
    mes        = this_month()
    pags_mes   = Pagamento.query.filter(
        Pagamento.tenant_id == tid(),
        Pagamento.data.startswith(mes)
    ).all()
    total_mes  = sum(p.valor for p in pags_mes)  # inclui estornos

    diarias    = [c for c in clientes if c.tipo_cobranca == 'diaria']
    mensais    = [c for c in clientes if c.tipo_cobranca == 'mensalidade']
    ativos_d   = [c for c in diarias if c.diarias_pagas < c.total_diarias]
    aguardando = [c for c in diarias if c.diarias_pagas >= c.total_diarias]
    em_atraso  = [c for c in clientes if c.dias_em_atraso > 0]

    ja_recebido = 0.0
    a_receber   = 0.0
    for c in clientes:
        if c.tipo_cobranca == 'diaria':
            ja_recebido += round(c.diarias_pagas * c.valor_diaria + c.saldo_pendente, 2)
            restantes    = max(0, c.total_diarias - c.diarias_pagas)
            a_receber   += round(restantes * c.valor_diaria - c.saldo_pendente, 2)
        else:
            parcela = c._parcela_mes_atual()
            if parcela:
                ja_recebido += parcela.valor_pago
                a_receber   += parcela.pendente

    ja_recebido     = max(0, round(ja_recebido, 2))
    a_receber       = max(0, round(a_receber, 2))
    total_projetado = round(ja_recebido + a_receber, 2)

    return render_template('dashboard.html',
        clientes=clientes, ativos=len(ativos_d), aguardando=len(aguardando),
        em_atraso=len(em_atraso), total_hoje=total_hoje, total_mes=total_mes,
        qtd_mensais=len(mensais), role=session['role'], hoje=today(),
        ja_recebido=round(ja_recebido, 2), a_receber=a_receber,
        total_projetado=total_projetado,
        tenant_nome=session.get('tenant_nome', '')
    )


@app.route('/clientes')
@login_required
def clientes():
    filtro = request.args.get('f', 'todos')
    tipo   = request.args.get('tipo', '')
    todos  = Cliente.query.filter_by(tenant_id=tid()).order_by(Cliente.criado_em.desc()).all()
    if filtro == 'ativos':    lista = [c for c in todos if c.ativo and c.status != 'aguardando']
    elif filtro == 'aguard':  lista = [c for c in todos if c.ativo and c.status == 'aguardando']
    elif filtro == 'atraso':  lista = [c for c in todos if c.ativo and c.dias_em_atraso > 0]
    elif filtro == 'arquiv':  lista = [c for c in todos if not c.ativo]
    else:                     lista = todos
    if tipo: lista = [c for c in lista if c.tipo_cobranca == tipo]
    return render_template('clientes.html', clientes=lista, filtro=filtro, tipo=tipo, role=session['role'])


@app.route('/cadastrar', methods=['GET', 'POST'])
@login_required
def cadastrar():
    if request.method == 'POST':
        nome          = request.form['nome'].strip()
        tipo_cobranca = request.form.get('tipo_cobranca', 'diaria')
        if not nome:
            flash('Nome obrigatório.', 'error')
            return redirect(url_for('cadastrar'))

        c = Cliente(
            tenant_id=tid(),
            nome=nome,
            whatsapp=request.form.get('whatsapp', '').strip(),
            cpf=request.form.get('cpf', '').strip(),
            limite=float(request.form.get('limite') or 0),
            endereco=request.form.get('endereco', '').strip(),
            email=request.form.get('email', '').strip(),
            chave_pix=request.form.get('chave_pix', '').strip(),
            tipo_cobranca=tipo_cobranca,
            juros_atraso=float(request.form.get('juros_atraso') or 0),
            obs_contrato=request.form.get('obs_contrato', '').strip(),
            data_inicio=request.form.get('data_inicio') or today(),
            foto_url=salvar_arquivo(request.files.get('foto')),
            arquivo_url=salvar_arquivo(request.files.get('arquivo')),
        )

        if tipo_cobranca == 'diaria':
            c.valor_diaria  = float(request.form.get('valor_diaria') or 0)
            c.total_diarias = int(request.form.get('total_diarias') or 20)
        else:
            c.valor_mensalidade   = float(request.form.get('valor_mensalidade') or 0)
            c.dia_vencimento      = int(request.form.get('dia_vencimento') or 10)
            c.cobranca_recorrente = request.form.get('cobranca_recorrente') == 'sim'

        db.session.add(c); db.session.commit()

        if tipo_cobranca == 'mensalidade':
            gerar_parcela_mes(c)

        flash(f'Cliente {nome} cadastrado!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('cadastrar.html', hoje=today())


@app.route('/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar(id):
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    if request.method == 'POST':
        c.nome=request.form['nome'].strip()
        c.whatsapp=request.form.get('whatsapp','').strip()
        c.cpf=request.form.get('cpf','').strip()
        c.limite=float(request.form.get('limite') or 0)
        c.endereco=request.form.get('endereco','').strip()
        c.email=request.form.get('email','').strip()
        c.chave_pix=request.form.get('chave_pix','').strip()
        c.juros_atraso=float(request.form.get('juros_atraso') or 0)
        c.obs_contrato=request.form.get('obs_contrato','').strip()
        if c.tipo_cobranca == 'diaria':
            c.valor_diaria=float(request.form.get('valor_diaria') or c.valor_diaria)
            c.total_diarias=int(request.form.get('total_diarias') or c.total_diarias)
            c.data_inicio=request.form.get('data_inicio') or c.data_inicio
        else:
            c.valor_mensalidade=float(request.form.get('valor_mensalidade') or c.valor_mensalidade)
            c.dia_vencimento=int(request.form.get('dia_vencimento') or c.dia_vencimento)
            c.cobranca_recorrente=request.form.get('cobranca_recorrente') == 'sim'
        foto=request.files.get('foto')
        if foto and foto.filename: c.foto_url=salvar_arquivo(foto)
        arquivo=request.files.get('arquivo')
        if arquivo and arquivo.filename: c.arquivo_url=salvar_arquivo(arquivo)
        db.session.commit(); flash('Cliente atualizado!', 'success')
        return redirect(url_for('dashboard'))
    pags    = Pagamento.query.filter_by(cliente_id=id, tenant_id=tid()).order_by(Pagamento.data.desc(), Pagamento.criado_em.desc()).all()
    parcelas= Parcela.query.filter_by(cliente_id=id, tenant_id=tid()).order_by(Parcela.competencia.desc()).all()
    hist    = ContratoHistorico.query.filter_by(cliente_id=id, tenant_id=tid()).order_by(ContratoHistorico.data_fim.desc()).all()
    return render_template('editar.html', c=c, pags=pags, parcelas=parcelas, hist=hist, role=session['role'])


@app.route('/apagar/<int:id>', methods=['POST'])
@login_required
@owner_required
def apagar(id):
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    nome = c.nome
    db.session.delete(c); db.session.commit()
    flash(f'Cliente {nome} apagado.', 'warn')
    return redirect(url_for('dashboard'))


# ── Pagamento DIÁRIA ───────────────────────────────────────────
@app.route('/pagar/<int:id>', methods=['POST'])
@login_required
def pagar(id):
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    valor=float(request.form['valor']); obs=request.form.get('obs','').strip()
    if valor<=0: flash('Valor inválido.','error'); return redirect(url_for('dashboard'))
    if not c.valor_diaria or c.valor_diaria<=0:
        flash('Valor da diária não configurado.','error'); return redirect(url_for('editar',id=id))
    saldo=c.saldo_pendente+valor; diarias_novas=int(saldo//c.valor_diaria)
    c.saldo_pendente=round(saldo%c.valor_diaria,2)
    diarias_novas=min(diarias_novas,c.total_diarias-c.diarias_pagas)
    c.diarias_pagas=min(c.total_diarias,c.diarias_pagas+diarias_novas)
    p=Pagamento(tenant_id=tid(), cliente_id=id, data=today(), valor=valor, diarias=diarias_novas, obs=obs)
    db.session.add(p); db.session.commit()
    if c.diarias_pagas>=c.total_diarias: flash(f'{c.nome} completou as {c.total_diarias} diárias! 🎉','success')
    elif diarias_novas==0: flash(f'R${valor:.2f} registrado. Faltam R${c.valor_diaria-c.saldo_pendente:.2f} pra próxima.','info')
    else: flash(f'+{diarias_novas} diária(s). Total: {c.diarias_pagas}/{c.total_diarias}.','success')
    return redirect(url_for('dashboard'))


# ── Pagamento MENSALIDADE ──────────────────────────────────────
@app.route('/pagar_mensalidade/<int:id>', methods=['POST'])
@login_required
def pagar_mensalidade(id):
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    valor=float(request.form['valor']); obs=request.form.get('obs','').strip()
    if valor<=0: flash('Valor inválido.','error'); return redirect(url_for('dashboard'))
    parcela=gerar_parcela_mes(c)
    if not parcela: flash('Erro ao encontrar parcela do mês.','error'); return redirect(url_for('dashboard'))
    parcela.valor_pago=round(parcela.valor_pago+valor,2)
    parcela.status='paga' if parcela.valor_pago>=parcela.valor else 'parcial'
    p=Pagamento(tenant_id=tid(), cliente_id=id, parcela_id=parcela.id, data=today(), valor=valor, obs=obs)
    db.session.add(p); db.session.commit()
    if parcela.status=='paga': flash(f'Mensalidade de {c.nome} quitada! ✅','success')
    else: flash(f'R${valor:.2f} registrado. Pendente: R${parcela.pendente:.2f}','info')
    return redirect(url_for('dashboard'))


# ── Estorno ────────────────────────────────────────────────────
@app.route('/estornar/<int:id>', methods=['POST'])
@login_required
def estornar(id):
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    valor=float(request.form.get('valor',0)); obs=request.form.get('obs','').strip()
    if valor<=0: flash('Valor inválido para remoção.','error'); return redirect(url_for('dashboard'))

    if c.tipo_cobranca=='diaria':
        qtd=int(valor//c.valor_diaria) if c.valor_diaria else 0
        qtd=max(1,min(qtd,c.diarias_pagas))
        c.diarias_pagas=max(0,c.diarias_pagas-qtd)
        c.saldo_pendente=max(0.0,c.saldo_pendente-(valor-qtd*c.valor_diaria))
        p=Pagamento(tenant_id=tid(), cliente_id=id, data=today(), valor=-valor, diarias=-qtd,
                    obs=f'[ESTORNO] {obs}' if obs else '[ESTORNO]')
        db.session.add(p); db.session.commit()
        flash(f'−R$ {valor:.2f} removido de {c.nome} (−{qtd} diária(s)).','warn')
    else:
        parcela=c._parcela_mes_atual()
        if parcela:
            parcela.valor_pago=max(0,round(parcela.valor_pago-valor,2))
            parcela.status='aberta' if parcela.valor_pago==0 else 'parcial'
        p=Pagamento(tenant_id=tid(), cliente_id=id,
                    parcela_id=parcela.id if parcela else None,
                    data=today(), valor=-valor, obs=f'[ESTORNO] {obs}' if obs else '[ESTORNO]')
        db.session.add(p); db.session.commit()
        flash(f'−R$ {valor:.2f} removido da mensalidade de {c.nome}.','warn')
    return redirect(url_for('dashboard'))


@app.route('/desfazer/<int:pag_id>', methods=['POST'])
@login_required
def desfazer(pag_id):
    p = Pagamento.query.filter_by(id=pag_id, tenant_id=tid()).first_or_404()
    c = p.cliente
    valor_abs = abs(p.valor)
    eh_estorno = p.valor < 0  # estorno tem valor negativo

    if c.tipo_cobranca == 'diaria':
        diarias_abs = abs(p.diarias or 0)
        if eh_estorno:
            # Desfazer um estorno = restaurar as diárias e saldo que foram removidos
            c.diarias_pagas = min(c.total_diarias, c.diarias_pagas + diarias_abs)
            c.saldo_pendente = round(c.saldo_pendente + (valor_abs - diarias_abs * c.valor_diaria), 2)
        else:
            # Desfazer um pagamento normal = remover diárias e saldo
            c.diarias_pagas = max(0, c.diarias_pagas - diarias_abs)
            c.saldo_pendente = max(0.0, c.saldo_pendente - (valor_abs - diarias_abs * c.valor_diaria))
    else:
        if p.parcela_id:
            parc = Parcela.query.filter_by(id=p.parcela_id, tenant_id=tid()).first()
            if parc:
                if eh_estorno:
                    # Desfazer estorno = restaurar o valor que foi removido da parcela
                    parc.valor_pago = round(parc.valor_pago + valor_abs, 2)
                else:
                    # Desfazer pagamento normal = subtrair da parcela
                    parc.valor_pago = max(0, round(parc.valor_pago - valor_abs, 2))
                parc.status = 'paga' if parc.valor_pago >= parc.valor else ('aberta' if parc.valor_pago == 0 else 'parcial')

    db.session.delete(p); db.session.commit()
    acao = 'Estorno' if eh_estorno else 'Pagamento'
    flash(f'{acao} de R$ {valor_abs:.2f} desfeito de {c.nome}.', 'warn')
    return redirect(url_for('editar', id=c.id))


@app.route('/renovar/<int:id>', methods=['POST'])
@login_required
def renovar(id):
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    novo_valor=float(request.form['valor_diaria']); nova_data=request.form.get('data_inicio') or today()
    hist=ContratoHistorico(tenant_id=tid(), cliente_id=id, data_inicio=c.data_inicio,
                           data_fim=today(), valor_diaria=c.valor_diaria, total_pago=c.total_pago)
    db.session.add(hist)
    c.diarias_pagas=0; c.saldo_pendente=0.0; c.valor_diaria=novo_valor
    c.data_inicio=nova_data; c.ativo=True
    db.session.commit(); flash(f'Contrato de {c.nome} renovado!','success')
    return redirect(url_for('dashboard'))


# ── Link público ───────────────────────────────────────────────
@app.route('/c/<token>')
def link_cliente(token):
    c = Cliente.query.filter_by(token_link=token).first_or_404()
    pags=sorted([p for p in c.pagamentos if p.valor>0 and p.data>=(c.data_inicio or '')],
                key=lambda p:(p.data,p.criado_em or ''),reverse=True)
    parcelas=Parcela.query.filter_by(cliente_id=c.id).order_by(Parcela.competencia.desc()).all()
    return render_template('link_cliente.html', c=c, pags=pags, parcelas=parcelas)


@app.route('/gerar_link/<int:id>', methods=['POST'])
@login_required
def gerar_link(id):
    import secrets
    c = Cliente.query.filter_by(id=id, tenant_id=tid()).first_or_404()
    c.token_link=secrets.token_urlsafe(32)
    db.session.commit(); flash('Novo link gerado!','success')
    return redirect(url_for('editar',id=id))


# ── Resumo ─────────────────────────────────────────────────────
@app.route('/resumo')
@login_required
def resumo():
    todos_pags=Pagamento.query.filter_by(tenant_id=tid()).order_by(Pagamento.data.desc(),Pagamento.criado_em.desc()).all()
    meses=sorted(set(p.data[:7] for p in todos_pags),reverse=True)
    mes_sel=request.args.get('mes',''); busca_nome=request.args.get('q','').strip().lower(); busca_dia=request.args.get('dia','').strip()
    dia_iso=''
    if busca_dia:
        try:
            if '/' in busca_dia:
                parts=busca_dia.replace(' ','').split('/')
                if len(parts)==3: d,m,a=parts; dia_iso=f"{a}-{m.zfill(2)}-{d.zfill(2)}"
            else: dia_iso=busca_dia
        except: dia_iso=''
    modo_geral=bool(busca_nome or dia_iso)
    if not mes_sel and not modo_geral: mes_sel=meses[0] if meses else this_month()
    pags_filtrados=list(todos_pags)  # inclui estornos
    if not modo_geral and mes_sel: pags_filtrados=[p for p in pags_filtrados if p.data.startswith(mes_sel)]
    if dia_iso: pags_filtrados=[p for p in pags_filtrados if p.data==dia_iso]
    todos_clientes=Cliente.query.filter_by(tenant_id=tid()).all()
    clientes_map={c.id:c for c in todos_clientes}
    if busca_nome:
        ids_match={c.id for c in todos_clientes if busca_nome in c.nome.lower()}
        pags_filtrados=[p for p in pags_filtrados if p.cliente_id in ids_match]
    pags_lista=sorted(pags_filtrados,key=lambda p:(p.data,p.criado_em.strftime('%H:%M:%S') if p.criado_em else ''),reverse=True)
    total_filtrado=round(sum(p.valor for p in pags_filtrados),2)
    return render_template('resumo.html',meses=meses,mes_sel=mes_sel,pags_lista=pags_lista,
        clientes_map=clientes_map,total_mes=total_filtrado,busca_nome=busca_nome,
        busca_dia=busca_dia,modo_geral=modo_geral,role=session['role'])


# ══════════════════════════════════════════════════════════════
# PAINEL ADMIN — acesso via ADMIN_KEY (só você usa isso)
# URL: /admin?key=SUA_CHAVE
# ══════════════════════════════════════════════════════════════

@app.route('/admin')
@admin_required
def admin_index():
    tenants = Tenant.query.order_by(Tenant.criado_em.desc()).all()
    key = request.args.get('key', '')
    stats = {}
    for t in tenants:
        stats[t.id] = Cliente.query.filter_by(tenant_id=t.id, ativo=True).count()
    return render_template('admin_tenants.html', tenants=tenants, stats=stats, key=key)


@app.route('/admin/criar', methods=['POST'])
@admin_required
def admin_criar_tenant():
    key   = request.form.get('key', '')
    nome  = request.form.get('nome', '').strip()
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '').strip()
    wpp   = request.form.get('wpp', '').strip() or WPP_SUPORTE

    if not nome or not email or not senha:
        flash('Nome, e-mail e senha são obrigatórios.', 'error')
        return redirect(url_for('admin_index', key=key))
    if Tenant.query.filter_by(email=email).first():
        flash('E-mail já cadastrado.', 'error')
        return redirect(url_for('admin_index', key=key))

    t = Tenant(nome=nome, email=email, wpp_suporte=wpp)
    t.set_senha(senha)
    db.session.add(t); db.session.commit()
    flash(f'Tenant "{nome}" criado com sucesso!', 'success')
    return redirect(url_for('admin_index', key=key))


@app.route('/admin/pausar/<int:tid_>', methods=['POST'])
@admin_required
def admin_pausar(tid_):
    key = request.form.get('key', '')
    t = Tenant.query.get_or_404(tid_)
    t.status = 'pausado'
    db.session.commit()
    flash(f'"{t.nome}" pausado.', 'warn')
    return redirect(url_for('admin_index', key=key))


@app.route('/admin/ativar/<int:tid_>', methods=['POST'])
@admin_required
def admin_ativar(tid_):
    key = request.form.get('key', '')
    t = Tenant.query.get_or_404(tid_)
    t.status = 'ativo'
    db.session.commit()
    flash(f'"{t.nome}" reativado!', 'success')
    return redirect(url_for('admin_index', key=key))


@app.route('/admin/senha/<int:tid_>', methods=['POST'])
@admin_required
def admin_senha(tid_):
    key   = request.form.get('key', '')
    nova  = request.form.get('nova_senha', '').strip()
    t = Tenant.query.get_or_404(tid_)
    if nova:
        t.set_senha(nova)
        db.session.commit()
        flash(f'Senha de "{t.nome}" atualizada.', 'success')
    return redirect(url_for('admin_index', key=key))


@app.route('/admin/deletar/<int:tid_>', methods=['POST'])
@admin_required
def admin_deletar(tid_):
    key = request.form.get('key', '')
    t = Tenant.query.get_or_404(tid_)
    nome = t.nome
    # Remove todos os dados do tenant em cascata
    Cliente.query.filter_by(tenant_id=tid_).delete()
    Pagamento.query.filter_by(tenant_id=tid_).delete()
    Parcela.query.filter_by(tenant_id=tid_).delete()
    ContratoHistorico.query.filter_by(tenant_id=tid_).delete()
    db.session.delete(t)
    db.session.commit()
    flash(f'Tenant "{nome}" e todos seus dados foram deletados.', 'warn')
    return redirect(url_for('admin_index', key=key))


# ══════════════════════════════════════════════════════════════
# APIs (bot WhatsApp) — filtradas por tenant via API key
# Nota: a API key identifica o tenant. Para multi-tenant com bot,
# cada tenant teria sua própria BOT_API_KEY. Por ora usa a global.
# ══════════════════════════════════════════════════════════════

def _tid_from_api():
    """Para APIs do bot: usa o tenant_id do header X-Tenant-Id, ou 1 como fallback."""
    tid_header = request.headers.get('X-Tenant-Id')
    if tid_header:
        try: return int(tid_header)
        except: pass
    # fallback: pega o primeiro tenant ativo (compatibilidade com bot antigo)
    t = Tenant.query.filter_by(status='ativo').first()
    return t.id if t else None


@app.route('/api/inadimplentes')
@api_key_required
def api_inadimplentes():
    atid = _tid_from_api()
    if not atid: return jsonify([])
    clientes=Cliente.query.filter_by(tenant_id=atid, ativo=True).all()
    for c in clientes:
        if c.tipo_cobranca=='mensalidade': gerar_parcela_mes(c)
    lista=[{'id':c.id,'nome':c.nome,'whatsapp':c.whatsapp,'tipo':c.tipo_cobranca,
            'dias_atraso':c.dias_em_atraso,'valor_atraso':c.valor_em_atraso,
            'diarias_pagas':c.diarias_pagas if c.tipo_cobranca=='diaria' else 0}
           for c in clientes if c.dias_em_atraso>0]
    return jsonify(lista)


@app.route('/api/stats')
@api_key_required
def api_stats():
    atid = _tid_from_api()
    if not atid: return jsonify(total_mes=0,total_hoje=0,em_atraso=0)
    mes=this_month()
    pags_mes=Pagamento.query.filter(Pagamento.tenant_id==atid, Pagamento.data.startswith(mes)).all()
    total_mes=sum(p.valor for p in pags_mes if p.valor>0)
    pags_hoje=Pagamento.query.filter_by(tenant_id=atid, data=today()).all()
    total_hoje=sum(p.valor for p in pags_hoje if p.valor>0)
    ativos=Cliente.query.filter_by(tenant_id=atid, ativo=True).all()
    em_atraso=len([c for c in ativos if c.dias_em_atraso>0])
    return jsonify(total_mes=total_mes,total_hoje=total_hoje,em_atraso=em_atraso)


@app.route('/api/clientes_ativos')
@api_key_required
def api_clientes_ativos():
    atid = _tid_from_api()
    if not atid: return jsonify([])
    clientes=Cliente.query.filter_by(tenant_id=atid, ativo=True).order_by(Cliente.nome).all()
    lista=[]
    for c in clientes:
        lista.append({'id':c.id,'nome':c.nome,'whatsapp':c.whatsapp or '',
            'cpf':c.cpf or '','tipo_cobranca':c.tipo_cobranca,
            'valor_diaria':c.valor_diaria,'valor_mensalidade':c.valor_mensalidade,
            'data_inicio':c.data_inicio or '','diarias_pagas':c.diarias_pagas,
            'total_diarias':c.total_diarias,'saldo_pendente':c.saldo_pendente,
            'dias_em_atraso':c.dias_em_atraso,'valor_em_atraso':c.valor_em_atraso,'status':c.status})
    return jsonify(lista)


@app.route('/api/cliente_por_whatsapp/<numero>')
@api_key_required
def api_cliente_por_whatsapp(numero):
    atid = _tid_from_api()
    if not atid: return jsonify(None), 404
    numero_limpo=re.sub(r'\D','',numero)
    if numero_limpo.startswith('55') and len(numero_limpo)>11: numero_limpo=numero_limpo[2:]
    for c in Cliente.query.filter_by(tenant_id=atid, ativo=True).all():
        if c.whatsapp:
            wa=re.sub(r'\D','',c.whatsapp)
            if len(wa)>=8 and len(numero_limpo)>=8 and wa[-8:]==numero_limpo[-8:]:
                return jsonify({'id':c.id,'nome':c.nome,'whatsapp':c.whatsapp,
                    'tipo_cobranca':c.tipo_cobranca,'valor_cobranca':c.valor_cobranca,
                    'diarias_pagas':c.diarias_pagas,'total_pago':c.total_pago,
                    'dias_em_atraso':c.dias_em_atraso,'valor_em_atraso':c.valor_em_atraso})
    return jsonify(None),404


@app.route('/api/verificar_comprovante', methods=['POST'])
@api_key_required
def api_verificar_comprovante():
    data=request.json or {}; codigo_tx=(data.get('codigo_tx') or '').strip()
    if not codigo_tx: return jsonify(duplicado=False,motivo='')
    p=Pagamento.query.filter(Pagamento.codigo_tx==codigo_tx,Pagamento.codigo_tx!='').first()
    if p: return jsonify(duplicado=True,motivo=f'TX já registrado (pag #{p.id} em {p.data})')
    return jsonify(duplicado=False,motivo='')


@app.route('/api/pagamentos_hoje/<int:id>')
@api_key_required
def api_pagamentos_hoje(id):
    atid = _tid_from_api()
    c=Cliente.query.filter_by(id=id, tenant_id=atid).first_or_404()
    if c.tipo_cobranca=='mensalidade':
        parcela=c._parcela_mes_atual()
        pago=parcela is not None and parcela.status=='paga'
    else:
        pago=Pagamento.query.filter_by(cliente_id=id,data=today()).filter(Pagamento.valor>0).first() is not None
    return jsonify(pagou_hoje=pago)


@app.route('/api/pagar/<int:id>', methods=['POST'])
@api_key_required
def api_pagar(id):
    atid = _tid_from_api()
    c=Cliente.query.filter_by(id=id, tenant_id=atid).first_or_404()
    data=request.json or {}
    valor=float(data.get('valor',0)); obs=data.get('obs','Pago via bot WhatsApp')
    hash_arq=data.get('hash_arquivo',''); codigo_tx=data.get('codigo_tx','')
    if valor<=0: return jsonify(erro='Valor inválido'),400

    if c.tipo_cobranca=='diaria':
        if not c.valor_diaria or c.valor_diaria<=0:
            return jsonify(erro='Cliente sem valor_diaria configurado'),400
        saldo=c.saldo_pendente+valor; diarias_novas=int(saldo//c.valor_diaria)
        c.saldo_pendente=round(saldo%c.valor_diaria,2)
        diarias_novas=min(diarias_novas,c.total_diarias-c.diarias_pagas)
        c.diarias_pagas=min(c.total_diarias,c.diarias_pagas+diarias_novas)
        p=Pagamento(tenant_id=atid, cliente_id=id, data=today(), valor=valor, diarias=diarias_novas,
                    obs=obs, hash_arquivo=hash_arq, codigo_tx=codigo_tx)
        db.session.add(p); db.session.commit()
        return jsonify(ok=True,pag_id=p.id,diarias_pagas=c.diarias_pagas,diarias_novas=diarias_novas,
            dias_em_atraso=c.dias_em_atraso,valor_em_atraso=c.valor_em_atraso)
    else:
        parcela=gerar_parcela_mes(c)
        parcela.valor_pago=round(parcela.valor_pago+valor,2)
        parcela.status='paga' if parcela.valor_pago>=parcela.valor else 'parcial'
        p=Pagamento(tenant_id=atid, cliente_id=id, parcela_id=parcela.id, data=today(), valor=valor,
                    obs=obs, hash_arquivo=hash_arq, codigo_tx=codigo_tx)
        db.session.add(p); db.session.commit()
        return jsonify(ok=True,pag_id=p.id,parcela_paga=parcela.status=='paga',
            valor_pago=parcela.valor_pago,pendente=parcela.pendente,
            dias_em_atraso=c.dias_em_atraso,valor_em_atraso=c.valor_em_atraso)


@app.route('/api/reverter/<int:pag_id>', methods=['POST'])
@api_key_required
def api_reverter(pag_id):
    atid = _tid_from_api()
    p=Pagamento.query.filter_by(id=pag_id, tenant_id=atid).first_or_404()
    c=p.cliente
    if c.tipo_cobranca=='diaria':
        c.diarias_pagas=max(0,c.diarias_pagas-p.diarias)
        c.saldo_pendente=max(0.0,c.saldo_pendente-(p.valor-p.diarias*c.valor_diaria))
    else:
        if p.parcela_id:
            parc=Parcela.query.filter_by(id=p.parcela_id, tenant_id=atid).first()
            if parc:
                parc.valor_pago=max(0,round(parc.valor_pago-p.valor,2))
                parc.status='aberta' if parc.valor_pago==0 else 'parcial'
    db.session.delete(p); db.session.commit()
    return jsonify(ok=True)


@app.route('/api/upsert_clientes', methods=['POST'])
@api_key_required
def api_upsert_clientes():
    atid = _tid_from_api()
    if not atid: return jsonify(erro='tenant não encontrado'), 400
    dados=request.json or {}; clientes_backup=dados.get('clientes',[])
    if not clientes_backup: return jsonify(erro='Lista vazia'),400
    cadastrados=atualizados=ignorados=0; erros=[]
    for item in clientes_backup:
        try:
            nome=(item.get('nome') or '').strip(); whatsapp=re.sub(r'\D','',item.get('whatsapp') or '')
            diarias_bkp=int(item.get('diarias_pagas') or 0)
            if not nome: continue
            cliente=None
            if whatsapp: cliente=Cliente.query.filter_by(tenant_id=atid, whatsapp=whatsapp, ativo=True).first()
            if not cliente: cliente=Cliente.query.filter(Cliente.tenant_id==atid, Cliente.nome.ilike(nome), Cliente.ativo==True).first()
            if cliente:
                if diarias_bkp>cliente.diarias_pagas: cliente.diarias_pagas=diarias_bkp; db.session.commit(); atualizados+=1
                else: ignorados+=1
            else:
                valor_diaria=float(item.get('valor_diaria') or 0)
                if not valor_diaria: erros.append(f'{nome}: sem valor_diaria'); continue
                novo=Cliente(tenant_id=atid, nome=nome, whatsapp=whatsapp or None,
                    cpf=item.get('cpf') or '', limite=float(item.get('limite') or 0),
                    endereco=item.get('endereco') or '', email=item.get('email') or '',
                    chave_pix=item.get('chave_pix') or '', tipo_cobranca='diaria',
                    valor_diaria=valor_diaria,
                    data_inicio=item.get('data_inicio') or date.today().isoformat(),
                    total_diarias=int(item.get('total_diarias') or 20),
                    diarias_pagas=diarias_bkp, saldo_pendente=float(item.get('saldo_pendente') or 0),
                    ativo=True)
                db.session.add(novo); db.session.commit(); cadastrados+=1
        except Exception as e: erros.append(f'{item.get("nome","?")} — {str(e)}')
    return jsonify(ok=True,cadastrados=cadastrados,atualizados=atualizados,ignorados=ignorados,erros=erros)


if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(debug=False)
