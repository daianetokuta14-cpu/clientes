from flask_sqlalchemy import SQLAlchemy
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


# ══════════════════════════════════════════════
# SISTEMA 1 — DIÁRIA (existente, sem mudança)
# ══════════════════════════════════════════════

class Cliente(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    nome            = db.Column(db.String(100), nullable=False)
    whatsapp        = db.Column(db.String(20))
    cpf             = db.Column(db.String(20))
    limite          = db.Column(db.Float, default=0.0)
    endereco        = db.Column(db.String(300))
    email           = db.Column(db.String(150))
    arquivo_url     = db.Column(db.Text)
    foto_url        = db.Column(db.Text)
    chave_pix       = db.Column(db.String(200))
    valor_diaria    = db.Column(db.Float, nullable=False)
    data_inicio     = db.Column(db.String(10), default=lambda: date.today().isoformat())
    diarias_pagas   = db.Column(db.Integer, default=0)
    saldo_pendente  = db.Column(db.Float, default=0.0)
    ativo           = db.Column(db.Boolean, default=True)
    token_link      = db.Column(db.String(48), unique=True, default=lambda: secrets.token_urlsafe(32))
    criado_em       = db.Column(db.DateTime, default=_now)
    pagamentos      = db.relationship('Pagamento', backref='cliente', lazy=True, cascade='all, delete-orphan')
    contratos       = db.relationship('ContratoHistorico', backref='cliente', lazy=True, cascade='all, delete-orphan')

    @property
    def status(self):
        if not self.ativo: return 'arquivado'
        if self.diarias_pagas >= 20: return 'aguardando'
        return 'ativo'

    @property
    def total_pago(self):
        if not self.data_inicio:
            return sum(p.valor for p in self.pagamentos)
        return sum(p.valor for p in self.pagamentos if p.data >= self.data_inicio)

    @property
    def pct(self):
        return min(100, round((self.diarias_pagas / 20) * 100))

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
        if not self.ativo or self.diarias_pagas >= 20: return 0
        esperado = min(self.dias_desde_inicio, 20)
        return max(0, esperado - self.diarias_pagas)

    @property
    def valor_em_atraso(self):
        return max(0, round(self.dias_em_atraso * self.valor_diaria - self.saldo_pendente, 2))


class Pagamento(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    cliente_id   = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    data         = db.Column(db.String(10), default=lambda: date.today().isoformat())
    valor        = db.Column(db.Float, nullable=False)
    diarias      = db.Column(db.Integer, default=0)
    obs          = db.Column(db.String(300), default='')
    hash_arquivo = db.Column(db.String(64), default='')
    codigo_tx    = db.Column(db.String(100), default='')
    criado_em    = db.Column(db.DateTime, default=_now)


class ContratoHistorico(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    cliente_id   = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    data_inicio  = db.Column(db.String(10))
    data_fim     = db.Column(db.String(10))
    valor_diaria = db.Column(db.Float)
    total_pago   = db.Column(db.Float, default=0)
    criado_em    = db.Column(db.DateTime, default=_now)


# ══════════════════════════════════════════════
# SISTEMA 2 — DIÁRIA + MENSALIDADE (novo)
# ══════════════════════════════════════════════

class ClienteV2(db.Model):
    __tablename__ = 'cliente_v2'

    id              = db.Column(db.Integer, primary_key=True)
    nome            = db.Column(db.String(100), nullable=False)
    whatsapp        = db.Column(db.String(20))
    cpf             = db.Column(db.String(20))
    endereco        = db.Column(db.String(300))
    email           = db.Column(db.String(150))
    foto_url        = db.Column(db.Text)
    arquivo_url     = db.Column(db.Text)
    chave_pix       = db.Column(db.String(200))
    token_link      = db.Column(db.String(48), unique=True, default=lambda: secrets.token_urlsafe(32))

    # Tipo de cobrança: 'diaria' ou 'mensalidade'
    tipo_cobranca   = db.Column(db.String(20), nullable=False, default='diaria')

    # ── Campos para DIÁRIA ──
    valor_diaria    = db.Column(db.Float, default=0.0)
    total_diarias   = db.Column(db.Integer, default=20)   # quantas diárias no contrato
    diarias_pagas   = db.Column(db.Integer, default=0)
    saldo_pendente  = db.Column(db.Float, default=0.0)

    # ── Campos para MENSALIDADE ──
    valor_mensalidade   = db.Column(db.Float, default=0.0)
    dia_vencimento      = db.Column(db.Integer, default=30)   # dia do mês (1-31)
    cobranca_recorrente = db.Column(db.Boolean, default=True)  # cobra todo mês

    # ── Juros por atraso (ambos os tipos) ──
    juros_atraso    = db.Column(db.Float, default=0.0)   # % ao dia ex: 1.0 = 1%

    # ── Status ──
    data_inicio     = db.Column(db.String(10), default=lambda: date.today().isoformat())
    ativo           = db.Column(db.Boolean, default=True)
    obs_contrato    = db.Column(db.Text, default='')

    criado_em       = db.Column(db.DateTime, default=_now)

    pagamentos_v2   = db.relationship('PagamentoV2', backref='cliente', lazy=True, cascade='all, delete-orphan')
    parcelas        = db.relationship('ParcelaV2', backref='cliente', lazy=True, cascade='all, delete-orphan')

    @property
    def status(self):
        if not self.ativo: return 'arquivado'
        if self.tipo_cobranca == 'diaria':
            if self.diarias_pagas >= self.total_diarias: return 'aguardando'
        return 'ativo'

    @property
    def total_pago(self):
        if not self.data_inicio:
            return sum(p.valor for p in self.pagamentos_v2)
        return sum(p.valor for p in self.pagamentos_v2 if p.data >= self.data_inicio)

    @property
    def pct(self):
        if self.tipo_cobranca == 'diaria':
            return min(100, round((self.diarias_pagas / max(1, self.total_diarias)) * 100))
        # mensalidade: % do mês atual pago
        parcela_atual = self._parcela_mes_atual()
        if parcela_atual and parcela_atual.valor_pago >= parcela_atual.valor:
            return 100
        return 0

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
            # mensalidade: verifica se parcela do mês está em atraso
            parcela = self._parcela_mes_atual()
            if not parcela: return 0
            hoje = date.today()
            venc = date(hoje.year, hoje.month, min(self.dia_vencimento, 28))
            if hoje > venc and parcela.valor_pago < parcela.valor:
                return (hoje - venc).days
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

    def _parcela_mes_atual(self):
        hoje = date.today()
        mes_atual = f"{hoje.year}-{hoje.month:02d}"
        for p in self.parcelas:
            if p.competencia == mes_atual:
                return p
        return None

    @property
    def valor_cobranca(self):
        """Valor principal de cobrança (diária ou mensalidade)"""
        if self.tipo_cobranca == 'diaria':
            return self.valor_diaria
        return self.valor_mensalidade


class PagamentoV2(db.Model):
    __tablename__ = 'pagamento_v2'

    id              = db.Column(db.Integer, primary_key=True)
    cliente_id      = db.Column(db.Integer, db.ForeignKey('cliente_v2.id'), nullable=False)
    parcela_id      = db.Column(db.Integer, db.ForeignKey('parcela_v2.id'), nullable=True)
    data            = db.Column(db.String(10), default=lambda: date.today().isoformat())
    valor           = db.Column(db.Float, nullable=False)
    tipo            = db.Column(db.String(20), default='normal')  # normal, juros, parcial
    obs             = db.Column(db.String(300), default='')
    hash_arquivo    = db.Column(db.String(64), default='')
    codigo_tx       = db.Column(db.String(100), default='')
    criado_em       = db.Column(db.DateTime, default=_now)


class ParcelaV2(db.Model):
    """Parcelas de mensalidade (1 por mês por cliente)"""
    __tablename__ = 'parcela_v2'

    id              = db.Column(db.Integer, primary_key=True)
    cliente_id      = db.Column(db.Integer, db.ForeignKey('cliente_v2.id'), nullable=False)
    competencia     = db.Column(db.String(7))   # ex: "2026-05"
    vencimento      = db.Column(db.String(10))  # ex: "2026-05-30"
    valor           = db.Column(db.Float, nullable=False)
    valor_pago      = db.Column(db.Float, default=0.0)
    status          = db.Column(db.String(20), default='aberta')  # aberta, paga, parcial, atrasada
    criado_em       = db.Column(db.DateTime, default=_now)
    pagamentos      = db.relationship('PagamentoV2', backref='parcela', lazy=True)

    @property
    def pendente(self):
        return max(0, round(self.valor - self.valor_pago, 2))
