from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime

db = SQLAlchemy()

def contar_dias_uteis_sem_domingo(inicio, fim):
    """Conta dias entre duas datas excluindo domingos"""
    total = 0
    atual = inicio
    while atual < fim:
        if atual.weekday() != 6:  # 6 = domingo
            total += 1
        atual = date.fromordinal(atual.toordinal() + 1)
    return total

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
    criado_em       = db.Column(db.DateTime, default=datetime.utcnow)
    pagamentos      = db.relationship('Pagamento', backref='cliente', lazy=True, cascade='all, delete-orphan')
    contratos       = db.relationship('ContratoHistorico', backref='cliente', lazy=True, cascade='all, delete-orphan')

    @property
    def status(self):
        if not self.ativo:
            return 'arquivado'
        if self.diarias_pagas >= 20:
            return 'aguardando'
        return 'ativo'

    @property
    def total_pago(self):
        """Soma apenas os pagamentos do contrato atual (a partir de data_inicio)."""
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
            # A primeira parcela só vence no dia SEGUINTE ao cadastro.
            # Ex: cliente entra dia 1 → primeira cobrança é dia 2.
            # Por isso somamos 1 dia ao início antes de contar.
            inicio_cobranca = date.fromordinal(inicio.toordinal() + 1)
            return contar_dias_uteis_sem_domingo(inicio_cobranca, date.today())
        except:
            return 0

    @property
    def dias_em_atraso(self):
        if not self.ativo or self.diarias_pagas >= 20:
            return 0
        esperado = min(self.dias_desde_inicio, 20)
        atraso = esperado - self.diarias_pagas
        return max(0, atraso)

    @property
    def valor_em_atraso(self):
        return max(0, round(self.dias_em_atraso * self.valor_diaria - self.saldo_pendente, 2))


class Pagamento(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    cliente_id     = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    data           = db.Column(db.String(10), default=lambda: date.today().isoformat())
    valor          = db.Column(db.Float, nullable=False)
    diarias        = db.Column(db.Integer, default=0)
    obs            = db.Column(db.String(300), default='')
    hash_arquivo   = db.Column(db.String(64), default='')   # SHA-256 do comprovante
    codigo_tx      = db.Column(db.String(100), default='')  # EndToEnd ID / TxID do PIX
    criado_em      = db.Column(db.DateTime, default=datetime.utcnow)


class ContratoHistorico(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    cliente_id   = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    data_inicio  = db.Column(db.String(10))
    data_fim     = db.Column(db.String(10))
    valor_diaria = db.Column(db.Float)
    total_pago   = db.Column(db.Float, default=0)
    criado_em    = db.Column(db.DateTime, default=datetime.utcnow)
