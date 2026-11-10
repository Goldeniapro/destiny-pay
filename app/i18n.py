"""Server-side texts in the three supported languages, plus the code-level yes/no classifier."""

from __future__ import annotations

import re
from datetime import date

LANGS = {"es": "Spanish", "en": "English", "pt": "Portuguese"}

T: dict[str, dict[str, str]] = {
    "busy": {"es": 'Un momento, todavía estoy procesando tu pedido anterior.', "en": "One moment, I'm still working on your previous request.", "pt": 'Um momento, ainda estou processando seu pedido anterior.'},
    "api_error": {"es": 'No pude conectarme con mi cerebro de IA. Repítelo en unos segundos, por favor.', "en": "I couldn't reach my AI service. Please try again in a few seconds.", "pt": 'Não consegui me conectar ao serviço de IA. Tente de novo em alguns segundos.'},
    "too_long": {"es": 'Ese pedido fue muy largo para mí. Dímelo por partes, por favor.', "en": 'That request was too long for me. Please say it in parts.', "pt": 'Esse pedido ficou longo demais. Diga em partes, por favor.'},
    "refusal": {"es": 'Lo siento, no puedo ayudar con ese pedido.', "en": "Sorry, I can't help with that request.", "pt": 'Desculpe, não posso ajudar com esse pedido.'},
    "conversation_full": {"es": 'Esta conversación llegó a su límite, así que empecé una nueva. Recuerdo todo lo de tu negocio.', "en": 'This conversation reached its limit, so I started a new one. I still remember everything about your business.', "pt": 'Esta conversa chegou ao limite, então comecei uma nova. Continuo lembrando tudo do seu negócio.'},
    "budget": {"es": 'La demo alcanzó su límite de uso por hoy. Vuelve mañana, por favor.', "en": 'The demo reached its usage limit for today. Please come back tomorrow.', "pt": 'A demo atingiu o limite de uso de hoje. Volte amanhã, por favor.'},
    "steps": {"es": 'Me tomó demasiados pasos. Pídemelo de nuevo en partes.', "en": 'That took me too many steps. Please ask again in parts.', "pt": 'Isso levou passos demais. Peça de novo em partes.'},
    "done": {"es": 'Listo.', "en": 'Done.', "pt": 'Pronto.'},
    "partial": {"es": 'Alcancé a hacer lo que ves marcado en verde, pero no pude terminar mi respuesta. Revisa el panel antes de repetir el pedido.', "en": "I completed the steps marked in green, but couldn't finish my reply. Check the dashboard before repeating the request.", "pt": 'Concluí as etapas marcadas em verde, mas não consegui terminar a resposta. Confira o painel antes de repetir o pedido.'},
    "c_send_invoice": {"es": 'Listo, le envié a {name} la factura de {total} con su link de pago.', "en": 'Done, I sent {name} the {total} invoice with its payment link.', "pt": 'Pronto, enviei a {name} a fatura de {total} com o link de pagamento.'},
    "c_cancel_invoice": {"es": 'Listo, cancelé la factura de {name} por {total}.', "en": "Done, I cancelled {name}'s {total} invoice.", "pt": 'Pronto, cancelei a fatura de {name} de {total}.'},
    "c_pay_bill": {"es": 'Listo, le pagué {amount} a {payee} con PayPal.', "en": 'Done, I paid {payee} {amount} with PayPal.', "pt": 'Pronto, paguei {amount} a {payee} com PayPal.'},
    "c_pay_bill_pending": {"es": 'Listo, envié el pago de {amount} a {payee} con PayPal. Te aviso cuando PayPal lo confirme.', "en": "Done, I sent the {amount} payment to {payee} with PayPal. I'll tell you when PayPal confirms it.", "pt": 'Pronto, enviei o pagamento de {amount} a {payee} com PayPal. Aviso quando o PayPal confirmar.'},
    "rb_send_invoice": {"es": 'Para confirmar: enviar la factura de {total} a {name}, al correo {email}. Di «sí» o toca Autorizar.', "en": 'To confirm: send {name} the {total} invoice at {email}. Say “yes” or tap Authorize.', "pt": 'Para confirmar: enviar a fatura de {total} para {name}, no e-mail {email}. Diga «sim» ou toque em Autorizar.'},
    "rb_cancel_invoice": {"es": 'Para confirmar: cancelar la factura de {total} de {name} ({email}). Di «sí» o toca Autorizar.', "en": "To confirm: cancel {name}'s {total} invoice ({email}). Say “yes” or tap Authorize.", "pt": 'Para confirmar: cancelar a fatura de {total} de {name} ({email}). Diga «sim» ou toque em Autorizar.'},
    "rb_pay_bill": {"es": 'Para confirmar: pagar {total} a {name} con PayPal, al correo {email}. Di «sí» o toca Autorizar.', "en": 'To confirm: pay {name} {total} with PayPal at {email}. Say “yes” or tap Authorize.', "pt": 'Para confirmar: pagar {total} a {name} com PayPal, no e-mail {email}. Diga «sim» ou toque em Autorizar.'},
    "button_only": {"es": 'Como el destinatario se anotó recién, autoriza este pago con el botón de la pantalla.', "en": 'Because the payee was added just now, authorize this payment with the button on screen.', "pt": 'Como o destinatário acabou de ser cadastrado, autorize este pagamento com o botão da tela.'},
    "button_only_new": {"es": 'Es el primer pago a este correo: revisa el correo y autorízalo con el botón de la pantalla.', "en": 'This is the first payment to this email: check the email and authorize it with the button on screen.', "pt": 'É o primeiro pagamento para este e-mail: confira o e-mail e autorize com o botão da tela.'},
    "multi_confirm": {"es": 'Preparé {n} operaciones. Por seguridad, confírmalas una por una con los botones de la pantalla.', "en": 'I prepared {n} operations. For safety, confirm them one by one with the buttons on screen.', "pt": 'Preparei {n} operações. Por segurança, confirme uma por uma com os botões da tela.'},
    "c_unverified": {"es": 'No lo ejecuté: no pude verificar la operación con PayPal. Inténtalo de nuevo en un momento.', "en": "I didn't run it: I couldn't verify the operation with PayPal. Please try again in a moment.", "pt": 'Não executei: não consegui verificar a operação com o PayPal. Tente de novo em instantes.'},
    "c_changed": {"es": 'No lo ejecuté: la operación cambió después de que la preparé. Pídemela de nuevo para revisarla otra vez.', "en": "I didn't run it: the operation changed after I prepared it. Ask me again so you can review it again.", "pt": 'Não executei: a operação mudou depois que eu a preparei. Peça de novo para revisá-la outra vez.'},
    "c_denied": {"es": 'Entendido, no lo hago.', "en": "Understood, I won't do it.", "pt": 'Entendido, não vou fazer.'},
    "c_failed": {"es": 'No pude hacerlo: {error}', "en": "I couldn't do it: {error}", "pt": 'Não consegui fazer: {error}'},
    "c_gone": {"es": 'Esa confirmación ya no está vigente. Pídemelo de nuevo, por favor.', "en": 'That confirmation is no longer valid. Please ask me again.', "pt": 'Essa confirmação não vale mais. Peça de novo, por favor.'},
    "a_invoice_paid": {"es": '¡{name} te pagó {total} con PayPal!', "en": '{name} just paid you {total} with PayPal!', "pt": '{name} acabou de te pagar {total} com PayPal!'},
    "a_reminder": {"es": 'Recordatorio: {text}', "en": 'Reminder: {text}', "pt": 'Lembrete: {text}'},
    "a_customer_reminded": {"es": 'Le recordé a {name} su factura de {total}, como me pediste.', "en": 'I reminded {name} about the {total} invoice, as you asked.', "pt": 'Lembrei {name} da fatura de {total}, como você pediu.'},
    "a_reminder_failed": {"es": 'No pude enviar el recordatorio a {name}: {error}', "en": "I couldn't send the reminder to {name}: {error}", "pt": 'Não consegui enviar o lembrete a {name}: {error}'},
    "a_payout_done": {"es": 'PayPal confirmó el pago de {amount} a {payee}.', "en": 'PayPal confirmed the {amount} payment to {payee}.', "pt": 'O PayPal confirmou o pagamento de {amount} a {payee}.'},
    "a_payout_unclaimed": {"es": 'El pago a {payee} salió, pero esa persona aún no lo reclama en PayPal.', "en": "The payment to {payee} went out, but they haven't claimed it in PayPal yet.", "pt": 'O pagamento a {payee} saiu, mas a pessoa ainda não o resgatou no PayPal.'},
    "a_payout_failed": {"es": 'El pago a {payee} no se completó en PayPal. Lo dejé otra vez como pendiente.', "en": "The payment to {payee} didn't go through in PayPal. I marked the bill as pending again.", "pt": 'O pagamento a {payee} não foi concluído no PayPal. Deixei a conta pendente de novo.'},
    "a_payout_unknown": {"es": 'No pude confirmar si el pago a {payee} salió. Revísalo en PayPal antes de pagarle de otra forma.', "en": "I couldn't confirm whether the payment to {payee} went out. Check PayPal before paying them another way.", "pt": 'Não consegui confirmar se o pagamento a {payee} saiu. Verifique no PayPal antes de pagar de outra forma.'},
    "a_payout_unclaimed_follow": {"es": '{payee} lleva {days} días sin reclamar el pago de {amount}. Confirma que su correo sea {email}: si nadie lo reclama en 30 días, PayPal te lo devuelve.', "en": "{payee} hasn't claimed the {amount} payment for {days} days. Check that their email is {email}: if nobody claims it within 30 days, PayPal returns it to you.", "pt": '{payee} está há {days} dias sem resgatar o pagamento de {amount}. Confirme se o e-mail é {email}: se ninguém resgatar em 30 dias, o PayPal devolve para você.'},
    "trust_note": {"es": 'Para verificar este cobro, inicia sesión en PayPal directamente. {business} nunca te pedirá llamar a un número de teléfono para pagar.', "en": 'To verify this request, sign in to PayPal directly. {business} will never ask you to call a phone number to pay.', "pt": 'Para verificar esta cobrança, entre no PayPal diretamente. {business} nunca vai pedir que você ligue para um número de telefone para pagar.'},
    "a_bill_due": {"es": 'Mañana vence el pago a {payee} por {amount}.', "en": 'The {amount} payment to {payee} is due tomorrow.', "pt": 'Amanhã vence o pagamento a {payee} de {amount}.'},
    "b_morning": {"es": 'Buenos días', "en": 'Good morning', "pt": 'Bom dia'},
    "b_afternoon": {"es": 'Buenas tardes', "en": 'Good afternoon', "pt": 'Boa tarde'},
    "b_evening": {"es": 'Buenas noches', "en": 'Good evening', "pt": 'Boa noite'},
    "b_receivable": {"es": 'Te deben {amount} en {n} cobros pendientes.', "en": "You're owed {amount} across {n} open invoices.", "pt": 'Te devem {amount} em {n} cobranças pendentes.'},
    "b_overdue": {"es": '{n} ya están vencidos.', "en": '{n} are overdue.', "pt": '{n} já estão vencidas.'},
    "b_bills": {"es": 'En los próximos 7 días tienes que pagar {amount}; lo primero es {payee} el {date}.', "en": 'In the next 7 days you have {amount} to pay; first is {payee} on {date}.', "pt": 'Nos próximos 7 dias você tem {amount} a pagar; o primeiro é {payee} em {date}.'},
    "b_bills_overdue": {"es": 'Tienes {n} pagos vencidos; el primero es {payee} por {amount}.', "en": 'You have {n} overdue bills; the first is {payee}, {amount}.', "pt": 'Você tem {n} pagamentos vencidos; o primeiro é {payee}, {amount}.'},
    "b_bills_overdue_one": {"es": 'Tienes 1 pago vencido: {payee} por {amount}.', "en": 'You have 1 overdue bill: {payee}, {amount}.', "pt": 'Você tem 1 pagamento vencido: {payee}, {amount}.'},
    "b_receivable_one": {"es": 'Te deben {amount} en 1 cobro pendiente.', "en": "You're owed {amount} on 1 open invoice.", "pt": 'Te devem {amount} em 1 cobrança pendente.'},
    "b_overdue_one": {"es": '1 ya está vencido.', "en": '1 is overdue.', "pt": '1 já está vencida.'},
    "b_reminders_one": {"es": 'Hoy tienes 1 recordatorio.', "en": 'You have 1 reminder today.', "pt": 'Hoje você tem 1 lembrete.'},
    "b_reminders": {"es": 'Hoy tienes {n} recordatorios.', "en": 'You have {n} reminders today.', "pt": 'Hoje você tem {n} lembretes.'},
    "b_month": {"es": 'Este mes llevas {income} de ingresos y {expense} de gastos.', "en": "This month you've had {income} in income and {expense} in expenses.", "pt": 'Este mês você teve {income} de receitas e {expense} de despesas.'},
    "b_clear": {"es": 'Todo está al día. ¿A quién le cobramos?', "en": 'Everything is up to date. Who should we bill?', "pt": 'Está tudo em dia. Quem vamos cobrar?'},
}

_DAYS = {"es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
         "pt": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]}

# --- activity log, shown in the owner's language ------------------------------------
LOG = {
    "invoice_draft": {"es": 'Borrador de factura para {name}: {amount}', "en": 'Draft invoice for {name}: {amount}', "pt": 'Rascunho de fatura para {name}: {amount}'},
    "invoice_paid": {"es": '{name} pagó {amount} en línea', "en": '{name} paid {amount} online', "pt": '{name} pagou {amount} online'},
    "invoice_sent": {"es": 'Factura enviada a {name} ({email})', "en": 'Invoice sent to {name} ({email})', "pt": 'Fatura enviada a {name} ({email})'},
    "invoice_cancelled": {"es": 'Factura de {name} cancelada', "en": 'Invoice for {name} cancelled', "pt": 'Fatura de {name} cancelada'},
    "invoice_reminded": {"es": 'Recordatorio de pago enviado a {name}', "en": 'Payment reminder sent to {name}', "pt": 'Lembrete de pagamento enviado a {name}'},
    "payment_recorded": {"es": '{name} pagó {amount} ({method})', "en": '{name} paid {amount} ({method})', "pt": '{name} pagou {amount} ({method})'},
    "bill_added": {"es": 'Cuenta por pagar: {payee} {amount} el {date}', "en": 'Bill to pay: {payee} {amount} on {date}', "pt": 'Conta a pagar: {payee} {amount} em {date}'},
    "bill_paid": {"es": 'Pagado a {payee}: {amount} ({method})', "en": 'Paid {payee} {amount} ({method})', "pt": 'Pago a {payee}: {amount} ({method})'},
    "bill_sent_unclaimed": {"es": 'Enviado a {payee} con PayPal, aún sin reclamar: {amount}', "en": 'Sent {payee} {amount} with PayPal, not claimed yet', "pt": 'Enviado a {payee} com PayPal, ainda não resgatado: {amount}'},
    "bill_paid_paypal": {"es": 'Pagado a {payee} con PayPal: {amount}', "en": 'Paid {payee} {amount} with PayPal', "pt": 'Pago a {payee} com PayPal: {amount}'},
    "payout_sent": {"es": 'Pago de PayPal enviado a {payee}: {amount}', "en": 'PayPal payment sent to {payee}: {amount}', "pt": 'Pagamento PayPal enviado a {payee}: {amount}'},
    "payout_failed": {"es": 'El pago de PayPal a {payee} falló; la cuenta vuelve a estar pendiente', "en": 'PayPal payment to {payee} failed; the bill is pending again', "pt": 'O pagamento PayPal a {payee} falhou; a conta voltou a pendente'},
    "payout_unknown": {"es": 'Pago a {payee} aún sin confirmar por PayPal', "en": 'Payment to {payee} not confirmed by PayPal yet', "pt": 'Pagamento a {payee} ainda não confirmado pelo PayPal'},
    "income": {"es": 'Ingreso: {desc} {amount}', "en": 'Income: {desc} {amount}', "pt": 'Receita: {desc} {amount}'},
    "expense": {"es": 'Gasto: {desc} {amount}', "en": 'Expense: {desc} {amount}', "pt": 'Despesa: {desc} {amount}'},
    "reminder_added": {"es": 'Recordatorio para {when}: {text}', "en": 'Reminder for {when}: {text}', "pt": 'Lembrete para {when}: {text}'},
    "reminder_cancelled": {"es": 'Recordatorio cancelado: {text}', "en": 'Reminder cancelled: {text}', "pt": 'Lembrete cancelado: {text}'},
    "fact": {"es": 'Recordaré: {fact}', "en": 'Remembered: {fact}', "pt": 'Vou lembrar: {fact}'},
    "demo": {"es": 'Negocio de ejemplo cargado', "en": 'Sample business loaded', "pt": 'Negócio de exemplo carregado'},
    "confirmation_denied": {"es": 'El dueño rechazó una acción', "en": 'The owner declined an action', "pt": 'O dono recusou uma ação'},
    "confirmation_failed": {"es": 'Una acción confirmada falló: {error}', "en": 'A confirmed action failed: {error}', "pt": 'Uma ação confirmada falhou: {error}'},
}


def log_text(key: str, lang: str, params: dict, fallback: str) -> str:
    tpl = LOG.get(key)
    if not tpl:
        return fallback
    try:
        return (tpl.get(lang) or tpl["en"]).format(**params)
    except (KeyError, IndexError):
        return fallback


def t(key: str, lang: str, **params) -> str:
    if params.get("n") == 1 and key + "_one" in T:  # singular wording ("1 pago vencido", not "1 pagos vencidos")
        key += "_one"
    text = T[key].get(lang) or T[key]["en"]
    return text.format(**params) if params else text


def fmt_money(amount: float, currency: str, lang: str = "en") -> str:
    text = f"{amount:,.2f}"
    if lang in ("es", "pt"):  # 1.234,56
        text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{text} {currency}"


def fmt_date(d: date, lang: str) -> str:
    day = _DAYS.get(lang, _DAYS["en"])[d.weekday()]
    if lang == "en":
        return f"{day} {d.month}/{d.day}"
    return f"{day} {d.day}/{d.month}"


# --- yes / no, decided by code ------------------------------------------------------
# A reply executes a money action only if EVERY word in it belongs to a small confirmation
# vocabulary ("sí", "dale, envíala", "yes please send it"). Anything else (a question, a number,
# a correction, another name, "send it to the other one") goes back to the agent. Any negation or
# "cancel" word means no, which is always safe: nothing moves.
_YES_WORDS = set("""
sí si dale confirmo confirmado confirma ok okay listo claro perfecto correcto adelante hazlo procede vale ya exacto
yes yeah yep yup sure confirm confirmed correct right perfect proceed absolutely ahead
sim pode confirmo confirmado confirma certo claro perfeito isso
""".split())
_FILLER_WORDS = set("""
bueno pues que por favor eso así de acuerdo destiny hazlo porfa
please it them that's thats fine great go do destiny now
favor agora pode
""".split())
_NO_WORDS = set("""
no nop nope espera espérate espere todavía detente nunca alto cancela cancelar cancélala cancélalo cancelalo cancelala anula
not don't dont wait stop never hold cancel cancelled
não nao pare ainda nunca cancele cancelar
""".split())
_CANCEL_WORDS = {"cancela", "cancelar", "cancélala", "cancélalo", "cancelalo", "cancelala", "anula", "cancel", "cancele"}
# A bare command that names the very action being confirmed is a yes ("envíala", "send it", "pay them", "pode mandar"),
# but only for that action: "págale" never approves sending an invoice.
_ACTION_YES = {
    "send_invoice": set("envíala envíalo envíale envía envia envíalos mándala mándalo manda mandala mándale mandar mande envie enviar send".split()),
    "pay_bill": set("págale pagale paga págalo pagalo pague pagar pay".split()),
}
# Action verbs are only ever accepted for their own action ("págale" never approves sending an invoice).
_ACTION_WORDS = {
    "send_invoice": _ACTION_YES["send_invoice"],
    "pay_bill": _ACTION_YES["pay_bill"],
    # "Cancelar" is also the label of the card's deny button: for a cancellation it is allowed next to a real
    # yes ("sí, cancélala") but never enough on its own.
    "cancel_invoice": _CANCEL_WORDS,
}


_NO_PROBLEM = re.compile(r"\b(?:no problem|no hay problema|sin problema|ningún problema|ningun problema|sem problema|não tem problema|nao tem problema)\b")


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'’-]+", text.lower().replace("’", "'"))


def classify_reply(text: str, action: str = "") -> str:
    """'yes', 'no' or 'other' for a reply to a pending confirmation of `action`."""
    raw = text.strip().lower()
    if not raw or any(q in raw for q in "?¿？؟⸮") or any(ch.isdigit() for ch in raw):
        return "other"
    # "no problem" is agreement, not a negation ("no problem, send it")
    raw = _NO_PROBLEM.sub(" ", raw)
    words = _words(raw)
    if not words:
        return "other"
    negatives = _NO_WORDS - (_CANCEL_WORDS if action == "cancel_invoice" else set())
    if set(words) & negatives:
        return "no"
    yes_words = _YES_WORDS | _ACTION_YES.get(action, set())
    allowed = yes_words | _FILLER_WORDS | _ACTION_WORDS.get(action, set())
    if len(words) <= 8 and all(w in allowed for w in words) and any(w in yes_words for w in words):
        return "yes"
    return "other"
