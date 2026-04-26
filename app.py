from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from models import db, Cliente, Pagamento, ContratoHistorico
from datetime import date, datetime
from functools import wraps
import os, base64

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///fincontrol.db').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fincontrol-chave-secreta-2025')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

PINS = {
    'owner':       os.environ.get('PIN_OWNER', '3670'),
    'funcionario': os.environ.get('PIN_FUNC',  '2930'),
}

db.init_app(app)

with app.app_context():
    db.create_all()

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
            flash('Apenas o owner pode executar esta acao.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def today():
    return date.today().isoformat()

def this_month():
    return date.today().strftime('%Y-%m')

def salvar_arquivo(file):
    """Converte arquivo para base64 para armazenar no banco"""
    if file and file.filename:
        data = file.read()
        b64 = base64.b64encode(data).decode('utf-8')
        mime = file.content_type or 'application/octet-stream'
        return f"data:{mime};base64,{b64}"
    return None

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'role' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        role = request.form.get('role')
        pin  = request.form.get('pin', '')
        if role in PINS and pin == PINS[role]:
            session['role'] = role
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
        flash('Valor invalido.', 'error')
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
        flash(f'{c.nome} completou as 20 diarias! Aguardando renovacao.', 'success')
    elif diarias_novas == 0:
        flash(f'R$ {valor:.2f} registrado. Saldo pendente: R$ {c.saldo_pendente:.2f} (faltam R$ {c.valor_diaria - c.saldo_pendente:.2f} para proxima diaria).', 'info')
    else:
        flash(f'+{diarias_novas} diaria(s) para {c.nome}. Total: {c.diarias_pagas}/20.', 'success')
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

@app.route('/api/inadimplentes')
@login_required
def api_inadimplentes():
    clientes = Cliente.query.filter_by(ativo=True).all()
    lista = []
    for c in clientes:
        if c.dias_em_atraso > 0:
            lista.append({
                'nome': c.nome,
                'whatsapp': c.whatsapp,
                'dias_atraso': c.dias_em_atraso,
                'valor_atraso': c.valor_em_atraso,
                'diarias_pagas': c.diarias_pagas
            })
    return jsonify(lista)

@app.route('/api/stats')
@login_required
def api_stats():
    mes = this_month()
    pags_mes = Pagamento.query.filter(Pagamento.data.startswith(mes)).all()
    total_mes = sum(p.valor for p in pags_mes)
    pags_hoje = Pagamento.query.filter_by(data=today()).all()
    total_hoje = sum(p.valor for p in pags_hoje)
    clientes_ativos = Cliente.query.filter_by(ativo=True).all()
    em_atraso = len([c for c in clientes_ativos if c.dias_em_atraso > 0])
    return jsonify(total_mes=total_mes, total_hoje=total_hoje, em_atraso=em_atraso)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False)
