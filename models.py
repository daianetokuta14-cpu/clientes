from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timezone, timedelta
import secrets

db = SQLAlchemy()

_TZ_MANAUS = timezone(timedelta(hours=-4))

def _now():
    return datetime.now(tz=_TZ_MANAUS).replace(tzinfo=None)

def contar_dias_uteis_sem_domingo(inicio, fim):
    total = 0
    atual = inicio
    while atual < fim:
        if atual.weekday() != 6:
            total += 1
        atual = date.fromordinal(atual.toordinal() + 1)
    return total


# ══════════════════════════════════════════════════════════════
# TENANT — cada cliente que compra o SaaS
# ══════════════════════════════════════════════════════════════
class Tenant(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    nome         = db.Column(db.String(100), nullable=False)
    email        = db.Column(db.String(150), unique=True, nullable=False)
    senha_hash   = db.Column(db.String(256), nullable=False)
    status       = db.Column(db.String(20), default='ativo')   # 'ativo' | 'pausado'
    wpp_suporte  = db.Column(db.String(30), default='')        # número WPP do suporte (seu)
    criado_em    = db.Column(db.DateTime, default=_now)

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def verificar_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)


# ══════════════════════════════════════════════════════════════
# CLIENTE — devedores cadastrados por cada tenant
# ══════════════════════════════════════════════════════════════
class Cliente(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    tenant_id       = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    nome            = db.Column(db.String(100), nullable=False)
    whatsapp        = db.Column(db.String(20))
    cpf             = db.Column(db.String(20))
    limite          = db.Column(db.Float, default=0.0)
    endereco        = db.Column(db.String(300))
    email           = db.Column(db.String(150))
    arquivo_url     = db.Column(db.Text)
    foto_url        = db.Column(db.Text)
    chave_pix       = db.Column(db.String(200))
    token_link      = db.Column(db.String(48), unique=True, default=lambda: secrets.token_urlsafe(32))
    criado_em       = db.Column(db.DateTime, default=_now)

    tipo_cobranca   = db.Column(db.String(20), nullable=False, default='diaria')

    # ── Campos DIÁRIA ──
    valor_diaria    = db.Column(db.Float, default=0.0)
    total_diarias   = db.Column(db.Integer, default=20)
    diarias_pagas   = db.Column(db.Integer, default=0)
    saldo_pendente  = db.Column(db.Float, default=0.0)
    data_inicio     = db.Column(db.String(10), default=lambda: date.today().isoformat())

    # ── Campos MENSALIDADE ──
    valor_mensalidade   = db.Column(db.Float, default=0.0)
    dia_vencimento      = db.Column(db.Integer, default=10)
    cobranca_recorrente = db.Column(db.Boolean, default=True)

    juros_atraso    = db.Column(db.Float, default=0.0)
    ativo           = db.Column(db.Boolean, default=True)
    obs_contrato    = db.Column(db.Text, default='')

    pagamentos  = db.relationship('Pagamento', backref='cliente', lazy=True, cascade='all, delete-orphan')
    contratos   = db.relationship('ContratoHistorico', backref='cliente', lazy=True, cascade='all, delete-orphan')
    parcelas    = db.relationship('Parcela', backref='cliente', lazy=True, cascade='all, delete-orphan')

    @property
    def dias_desde_inicio(self):
        try:
            inicio = date.fromisoformat(self.data_inicio)
            inicio_cobranca = date.fromordinal(inicio.toordinal() + 1)
            return contar_dias_uteis_sem_domingo(inicio_cobranca, date.today())
        except:
            return 0

    @property
    def dias_em_atraso(self):
        if not self.ativo: return 0
        if self.tipo_cobranca == 'diaria':
            if self.diarias_pagas >= self.total_diarias: return 0
            esperado = min(self.dias_desde_inicio, self.total_diarias)
            return max(0, esperado - self.diarias_pagas)
        else:
            parcela = self._parcela_mes_atual()
            if not parcela: return 0
            hoje = date.today()
            try:
                venc = date.fromisoformat(parcela.vencimento)
                if hoje > venc and parcela.valor_pago < parcela.valor:
                    return (hoje - venc).days
            except:
                pass
            return 0

    @property
    def valor_em_atraso(self):
        if self.tipo_cobranca == 'diaria':
            base = max(0, self.dias_em_atraso * self.valor_diaria - self.saldo_pendente)
            juros = base * (self.juros_atraso / 100) * self.dias_em_atraso if self.juros_atraso and self.dias_em_atraso else 0
            return round(base + juros, 2)
        else:
            parcela = self._parcela_mes_atual()
            if not parcela: return 0
            pendente = max(0, parcela.valor - parcela.valor_pago)
            juros = pendente * (self.juros_atraso / 100) * self.dias_em_atraso if self.juros_atraso and self.dias_em_atraso else 0
            return round(pendente + juros, 2)

    @property
    def status(self):
        if not self.ativo: return 'arquivado'
        if self.tipo_cobranca == 'diaria':
            if self.diarias_pagas >= self.total_diarias: return 'aguardando'
        return 'ativo'

    @property
    def total_pago(self):
        if self.tipo_cobranca == 'diaria':
            if not self.data_inicio:
                return sum(p.valor for p in self.pagamentos if p.valor > 0)
            return sum(p.valor for p in self.pagamentos if p.valor > 0 and p.data >= self.data_inicio)
        return sum(p.valor for p in self.pagamentos if p.valor > 0)

    @property
    def pct(self):
        if self.tipo_cobranca == 'diaria':
            return min(100, round((self.diarias_pagas / max(1, self.total_diarias)) * 100))
        parcela = self._parcela_mes_atual()
        if parcela and parcela.valor > 0:
            return min(100, round((parcela.valor_pago / parcela.valor) * 100))
        return 0

    @property
    def valor_cobranca(self):
        if self.tipo_cobranca == 'diaria':
            return self.valor_diaria
        return self.valor_mensalidade

    def _parcela_mes_atual(self):
        hoje = date.today()
        mes_atual = f"{hoje.year}-{hoje.month:02d}"
        for p in self.parcelas:
            if p.competencia == mes_atual:
                return p
        return None


class Pagamento(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    tenant_id    = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    cliente_id   = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    parcela_id   = db.Column(db.Integer, db.ForeignKey('parcela.id'), nullable=True)
    data         = db.Column(db.String(10), default=lambda: date.today().isoformat())
    valor        = db.Column(db.Float, nullable=False)
    diarias      = db.Column(db.Integer, default=0)
    obs          = db.Column(db.String(300), default='')
    hash_arquivo = db.Column(db.String(64), default='')
    codigo_tx    = db.Column(db.String(100), default='')
    criado_em    = db.Column(db.DateTime, default=_now)


class Parcela(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    cliente_id  = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    competencia = db.Column(db.String(7))
    vencimento  = db.Column(db.String(10))
    valor       = db.Column(db.Float, nullable=False)
    valor_pago  = db.Column(db.Float, default=0.0)
    status      = db.Column(db.String(20), default='aberta')
    criado_em   = db.Column(db.DateTime, default=_now)
    pagamentos  = db.relationship('Pagamento', backref='parcela', lazy=True)

    @property
    def pendente(self):
        return max(0, round(self.valor - self.valor_pago, 2))


class ContratoHistorico(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    tenant_id    = db.Column(db.Integer, db.ForeignKey('tenant.id'), nullable=False)
    cliente_id   = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    data_inicio  = db.Column(db.String(10))
    data_fim     = db.Column(db.String(10))
    valor_diaria = db.Column(db.Float)
    total_pago   = db.Column(db.Float, default=0)
    criado_em    = db.Column(db.DateTime, default=_now)
