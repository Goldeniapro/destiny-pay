"""Server-side texts in the five supported languages, plus the code-level yes/no classifier."""

from __future__ import annotations

import re
from datetime import date

LANGS = {"es": "Spanish", "en": "English", "pt": "Portuguese", "zh": "Mandarin Chinese", "fr": "French"}

T: dict[str, dict[str, str]] = {
    # --- agent loop ----------------------------------------------------------
    "busy": {"es": "Un momento, todavía estoy procesando tu pedido anterior.", "en": "One moment, I'm still working on your previous request.",
             "pt": "Um momento, ainda estou processando seu pedido anterior.", "zh": "请稍等，我还在处理你上一个请求。", "fr": "Un instant, je traite encore votre demande précédente."},
    "api_error": {"es": "No pude conectarme con mi cerebro de IA. Repítelo en unos segundos, por favor.", "en": "I couldn't reach my AI service. Please try again in a few seconds.",
                  "pt": "Não consegui me conectar ao serviço de IA. Tente de novo em alguns segundos.", "zh": "我暂时连不上 AI 服务，请几秒后再试。", "fr": "Je n'arrive pas à joindre mon service d'IA. Réessayez dans quelques secondes."},
    "too_long": {"es": "Ese pedido fue muy largo para mí. Dímelo por partes, por favor.", "en": "That request was too long for me. Please say it in parts.",
                 "pt": "Esse pedido ficou longo demais. Diga em partes, por favor.", "zh": "这个请求太长了，请分几次告诉我。", "fr": "Cette demande est trop longue. Dites-la en plusieurs fois, s'il vous plaît."},
    "refusal": {"es": "Lo siento, no puedo ayudar con ese pedido.", "en": "Sorry, I can't help with that request.", "pt": "Desculpe, não posso ajudar com esse pedido.",
                "zh": "抱歉，这个请求我帮不了。", "fr": "Désolée, je ne peux pas aider avec cette demande."},
    "conversation_full": {"es": "Esta conversación llegó a su límite, así que empecé una nueva. Recuerdo todo lo de tu negocio.",
                          "en": "This conversation reached its limit, so I started a new one. I still remember everything about your business.",
                          "pt": "Esta conversa chegou ao limite, então comecei uma nova. Continuo lembrando tudo do seu negócio.",
                          "zh": "这段对话已达上限，我开了一个新的。你生意的一切我都还记得。",
                          "fr": "Cette conversation a atteint sa limite, j'en ai commencé une nouvelle. Je me souviens de tout sur votre commerce."},
    "budget": {"es": "La demo alcanzó su límite de uso por hoy. Vuelve mañana, por favor.", "en": "The demo reached its usage limit for today. Please come back tomorrow.",
               "pt": "A demo atingiu o limite de uso de hoje. Volte amanhã, por favor.", "zh": "演示今天的使用量已达上限，请明天再来。", "fr": "La démo a atteint sa limite d'utilisation du jour. Revenez demain."},
    "steps": {"es": "Me tomó demasiados pasos. Pídemelo de nuevo en partes.", "en": "That took me too many steps. Please ask again in parts.",
              "pt": "Isso levou passos demais. Peça de novo em partes.", "zh": "步骤太多了，请分开再问我。", "fr": "Cela m'a demandé trop d'étapes. Redemandez en plusieurs fois."},
    "done": {"es": "Listo.", "en": "Done.", "pt": "Pronto.", "zh": "好了。", "fr": "C'est fait."},
    "partial": {"es": "Alcancé a hacer lo que ves marcado en verde, pero no pude terminar mi respuesta. Revisa el panel antes de repetir el pedido.",
                "en": "I completed the steps marked in green, but couldn't finish my reply. Check the dashboard before repeating the request.",
                "pt": "Concluí as etapas marcadas em verde, mas não consegui terminar a resposta. Confira o painel antes de repetir o pedido.",
                "zh": "绿色标记的步骤已经完成，但我没能说完回复。重复请求前请先看一下面板。",
                "fr": "J'ai terminé les étapes en vert, mais je n'ai pas pu finir ma réponse. Vérifiez le tableau avant de répéter la demande."},
    # --- confirmations (executed by code, never by the model) ------------------
    "c_send_invoice": {"es": "Listo, le envié a {name} la factura de {total} con su link de pago.", "en": "Done, I sent {name} the {total} invoice with its payment link.",
                       "pt": "Pronto, enviei a {name} a fatura de {total} com o link de pagamento.", "zh": "好了，我已把 {total} 的账单和付款链接发给 {name}。",
                       "fr": "C'est fait, j'ai envoyé à {name} la facture de {total} avec son lien de paiement."},
    "c_cancel_invoice": {"es": "Listo, cancelé la factura de {name} por {total}.", "en": "Done, I cancelled {name}'s {total} invoice.",
                         "pt": "Pronto, cancelei a fatura de {name} de {total}.", "zh": "好了，我已取消 {name} 的 {total} 账单。", "fr": "C'est fait, j'ai annulé la facture de {name} de {total}."},
    "c_pay_bill": {"es": "Listo, le pagué {amount} a {payee} con PayPal.", "en": "Done, I paid {payee} {amount} with PayPal.",
                   "pt": "Pronto, paguei {amount} a {payee} com PayPal.", "zh": "好了，我已用 PayPal 向 {payee} 支付 {amount}。", "fr": "C'est fait, j'ai payé {amount} à {payee} avec PayPal."},
    "c_pay_bill_pending": {"es": "Listo, envié el pago de {amount} a {payee} con PayPal. Te aviso cuando PayPal lo confirme.",
                           "en": "Done, I sent the {amount} payment to {payee} with PayPal. I'll tell you when PayPal confirms it.",
                           "pt": "Pronto, enviei o pagamento de {amount} a {payee} com PayPal. Aviso quando o PayPal confirmar.",
                           "zh": "好了，我已用 PayPal 向 {payee} 发出 {amount} 的付款，PayPal 确认后我会告诉你。",
                           "fr": "C'est fait, j'ai envoyé le paiement de {amount} à {payee} avec PayPal. Je vous préviens dès que PayPal le confirme."},
    "c_denied": {"es": "Entendido, no lo hago.", "en": "Understood, I won't do it.", "pt": "Entendido, não vou fazer.", "zh": "明白，我不执行。", "fr": "Compris, je ne le fais pas."},
    "c_failed": {"es": "No pude hacerlo: {error}", "en": "I couldn't do it: {error}", "pt": "Não consegui fazer: {error}", "zh": "没能完成：{error}", "fr": "Je n'ai pas pu le faire : {error}"},
    "c_gone": {"es": "Esa confirmación ya no está vigente. Pídemelo de nuevo, por favor.", "en": "That confirmation is no longer valid. Please ask me again.",
               "pt": "Essa confirmação não vale mais. Peça de novo, por favor.", "zh": "这个确认已失效，请重新告诉我。", "fr": "Cette confirmation n'est plus valable. Redemandez-moi, s'il vous plaît."},
    # --- alerts (spoken by the dashboard) ---------------------------------------
    "a_invoice_paid": {"es": "¡{name} te pagó {total} con PayPal!", "en": "{name} just paid you {total} with PayPal!", "pt": "{name} acabou de te pagar {total} com PayPal!",
                       "zh": "{name} 刚刚用 PayPal 付了你 {total}！", "fr": "{name} vient de vous payer {total} avec PayPal !"},
    "a_reminder": {"es": "Recordatorio: {text}", "en": "Reminder: {text}", "pt": "Lembrete: {text}", "zh": "提醒：{text}", "fr": "Rappel : {text}"},
    "a_customer_reminded": {"es": "Le recordé a {name} su factura de {total}, como me pediste.", "en": "I reminded {name} about the {total} invoice, as you asked.",
                            "pt": "Lembrei {name} da fatura de {total}, como você pediu.", "zh": "已按你的要求提醒 {name} 支付 {total} 的账单。", "fr": "J'ai rappelé à {name} sa facture de {total}, comme demandé."},
    "a_reminder_failed": {"es": "No pude enviar el recordatorio a {name}: {error}", "en": "I couldn't send the reminder to {name}: {error}",
                          "pt": "Não consegui enviar o lembrete a {name}: {error}", "zh": "没能给 {name} 发送提醒：{error}", "fr": "Je n'ai pas pu envoyer le rappel à {name} : {error}"},
    "a_payout_done": {"es": "PayPal confirmó el pago de {amount} a {payee}.", "en": "PayPal confirmed the {amount} payment to {payee}.",
                      "pt": "O PayPal confirmou o pagamento de {amount} a {payee}.", "zh": "PayPal 已确认向 {payee} 支付 {amount}。", "fr": "PayPal a confirmé le paiement de {amount} à {payee}."},
    "a_payout_unclaimed": {"es": "El pago a {payee} salió, pero esa persona aún no lo reclama en PayPal.", "en": "The payment to {payee} went out, but they haven't claimed it in PayPal yet.",
                           "pt": "O pagamento a {payee} saiu, mas a pessoa ainda não o resgatou no PayPal.", "zh": "给 {payee} 的付款已发出，但对方还没在 PayPal 领取。",
                           "fr": "Le paiement à {payee} est parti, mais le bénéficiaire ne l'a pas encore réclamé sur PayPal."},
    "a_payout_failed": {"es": "El pago a {payee} no se completó en PayPal. Lo dejé otra vez como pendiente.", "en": "The payment to {payee} didn't go through in PayPal. I marked the bill as pending again.",
                        "pt": "O pagamento a {payee} não foi concluído no PayPal. Deixei a conta pendente de novo.", "zh": "给 {payee} 的付款在 PayPal 未完成，我已把这笔账单改回待付。",
                        "fr": "Le paiement à {payee} n'a pas abouti sur PayPal. J'ai remis la facture en attente."},
    "a_payout_unknown": {"es": "No pude confirmar si el pago a {payee} salió. Revísalo en PayPal antes de pagarle de otra forma.",
                         "en": "I couldn't confirm whether the payment to {payee} went out. Check PayPal before paying them another way.",
                         "pt": "Não consegui confirmar se o pagamento a {payee} saiu. Verifique no PayPal antes de pagar de outra forma.",
                         "zh": "我无法确认给 {payee} 的付款是否已发出，请先在 PayPal 查看，再用其他方式付款。",
                         "fr": "Je n'ai pas pu confirmer si le paiement à {payee} est parti. Vérifiez sur PayPal avant de payer autrement."},
    "a_bill_due": {"es": "Mañana vence el pago a {payee} por {amount}.", "en": "The {amount} payment to {payee} is due tomorrow.",
                   "pt": "Amanhã vence o pagamento a {payee} de {amount}.", "zh": "明天要付给 {payee} {amount}。", "fr": "Le paiement de {amount} à {payee} est dû demain."},
    # --- daily briefing ----------------------------------------------------------
    "b_morning": {"es": "Buenos días", "en": "Good morning", "pt": "Bom dia", "zh": "早上好", "fr": "Bonjour"},
    "b_afternoon": {"es": "Buenas tardes", "en": "Good afternoon", "pt": "Boa tarde", "zh": "下午好", "fr": "Bonjour"},
    "b_evening": {"es": "Buenas noches", "en": "Good evening", "pt": "Boa noite", "zh": "晚上好", "fr": "Bonsoir"},
    "b_receivable": {"es": "Te deben {amount} en {n} cobros pendientes.", "en": "You're owed {amount} across {n} open invoices.",
                     "pt": "Te devem {amount} em {n} cobranças pendentes.", "zh": "你有 {n} 笔待收款，共 {amount}。", "fr": "On vous doit {amount} sur {n} factures en attente."},
    "b_overdue": {"es": "{n} ya están vencidos.", "en": "{n} are overdue.", "pt": "{n} já estão vencidas.", "zh": "其中 {n} 笔已逾期。", "fr": "{n} sont en retard."},
    "b_bills": {"es": "En los próximos 7 días tienes que pagar {amount}; lo primero es {payee} el {date}.",
                "en": "In the next 7 days you have {amount} to pay; first is {payee} on {date}.",
                "pt": "Nos próximos 7 dias você tem {amount} a pagar; o primeiro é {payee} em {date}.",
                "zh": "未来 7 天你要付 {amount}，最先到期的是 {date} 的 {payee}。",
                "fr": "Dans les 7 prochains jours, vous avez {amount} à payer ; d'abord {payee} le {date}."},
    "b_bills_overdue": {"es": "Tienes {n} pago vencido: {payee} por {amount}.", "en": "You have {n} overdue bill: {payee}, {amount}.",
                        "pt": "Você tem {n} pagamento vencido: {payee}, {amount}.", "zh": "你有 {n} 笔逾期账单：{payee}，{amount}。",
                        "fr": "Vous avez {n} paiement en retard : {payee}, {amount}."},
    "b_reminders": {"es": "Hoy tienes {n} recordatorios.", "en": "You have {n} reminders today.", "pt": "Hoje você tem {n} lembretes.", "zh": "今天你有 {n} 个提醒。", "fr": "Vous avez {n} rappels aujourd'hui."},
    "b_month": {"es": "Este mes llevas {income} de ingresos y {expense} de gastos.", "en": "This month you've had {income} in income and {expense} in expenses.",
                "pt": "Este mês você teve {income} de receitas e {expense} de despesas.", "zh": "本月收入 {income}，支出 {expense}。", "fr": "Ce mois-ci : {income} de recettes et {expense} de dépenses."},
    "b_clear": {"es": "Todo está al día. ¿A quién le cobramos?", "en": "Everything is up to date. Who should we bill?", "pt": "Está tudo em dia. Quem vamos cobrar?",
                "zh": "一切都已处理好。要向谁收款？", "fr": "Tout est à jour. Qui facturons-nous ?"},
}

_DAYS = {"es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
         "pt": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"], "zh": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
         "fr": ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]}

# --- activity log, shown in the owner's language ------------------------------------
LOG = {
    "invoice_draft": {"es": 'Borrador de factura para {name}: {amount}', "en": 'Draft invoice for {name}: {amount}', "pt": 'Rascunho de fatura para {name}: {amount}', "zh": '为 {name} 创建账单草稿：{amount}', "fr": 'Brouillon de facture pour {name} : {amount}'},
    "invoice_paid": {"es": '{name} pagó {amount} en línea', "en": '{name} paid {amount} online', "pt": '{name} pagou {amount} online', "zh": '{name} 在线支付了 {amount}', "fr": '{name} a payé {amount} en ligne'},
    "invoice_sent": {"es": 'Factura enviada a {name} ({email})', "en": 'Invoice sent to {name} ({email})', "pt": 'Fatura enviada a {name} ({email})', "zh": '账单已发送给 {name}（{email}）', "fr": 'Facture envoyée à {name} ({email})'},
    "invoice_cancelled": {"es": 'Factura de {name} cancelada', "en": 'Invoice for {name} cancelled', "pt": 'Fatura de {name} cancelada', "zh": '{name} 的账单已取消', "fr": 'Facture de {name} annulée'},
    "invoice_reminded": {"es": 'Recordatorio de pago enviado a {name}', "en": 'Payment reminder sent to {name}', "pt": 'Lembrete de pagamento enviado a {name}', "zh": '已向 {name} 发送付款提醒', "fr": 'Rappel de paiement envoyé à {name}'},
    "payment_recorded": {"es": '{name} pagó {amount} ({method})', "en": '{name} paid {amount} ({method})', "pt": '{name} pagou {amount} ({method})', "zh": '{name} 支付了 {amount}（{method}）', "fr": '{name} a payé {amount} ({method})'},
    "bill_added": {"es": 'Cuenta por pagar: {payee} {amount} el {date}', "en": 'Bill to pay: {payee} {amount} on {date}', "pt": 'Conta a pagar: {payee} {amount} em {date}', "zh": '应付账单：{payee} {amount}，{date} 到期', "fr": 'Facture à payer : {payee} {amount} le {date}'},
    "bill_paid": {"es": 'Pagado a {payee}: {amount} ({method})', "en": 'Paid {payee} {amount} ({method})', "pt": 'Pago a {payee}: {amount} ({method})', "zh": '已付给 {payee} {amount}（{method}）', "fr": 'Payé à {payee} : {amount} ({method})'},
    "bill_paid_paypal": {"es": 'Pagado a {payee} con PayPal: {amount}', "en": 'Paid {payee} {amount} with PayPal', "pt": 'Pago a {payee} com PayPal: {amount}', "zh": '已用 PayPal 付给 {payee} {amount}', "fr": 'Payé à {payee} avec PayPal : {amount}'},
    "payout_sent": {"es": 'Pago de PayPal enviado a {payee}: {amount}', "en": 'PayPal payment sent to {payee}: {amount}', "pt": 'Pagamento PayPal enviado a {payee}: {amount}', "zh": '已向 {payee} 发出 PayPal 付款：{amount}', "fr": 'Paiement PayPal envoyé à {payee} : {amount}'},
    "payout_failed": {"es": 'El pago de PayPal a {payee} falló; la cuenta vuelve a estar pendiente', "en": 'PayPal payment to {payee} failed; the bill is pending again', "pt": 'O pagamento PayPal a {payee} falhou; a conta voltou a pendente', "zh": '给 {payee} 的 PayPal 付款失败，账单恢复为待付', "fr": 'Le paiement PayPal à {payee} a échoué ; la facture est de nouveau à payer'},
    "payout_unknown": {"es": 'Pago a {payee} aún sin confirmar por PayPal', "en": 'Payment to {payee} not confirmed by PayPal yet', "pt": 'Pagamento a {payee} ainda não confirmado pelo PayPal', "zh": '给 {payee} 的付款尚未获 PayPal 确认', "fr": 'Paiement à {payee} pas encore confirmé par PayPal'},
    "income": {"es": 'Ingreso: {desc} {amount}', "en": 'Income: {desc} {amount}', "pt": 'Receita: {desc} {amount}', "zh": '收入：{desc} {amount}', "fr": 'Recette : {desc} {amount}'},
    "expense": {"es": 'Gasto: {desc} {amount}', "en": 'Expense: {desc} {amount}', "pt": 'Despesa: {desc} {amount}', "zh": '支出：{desc} {amount}', "fr": 'Dépense : {desc} {amount}'},
    "reminder_added": {"es": 'Recordatorio para {when}: {text}', "en": 'Reminder for {when}: {text}', "pt": 'Lembrete para {when}: {text}', "zh": '提醒（{when}）：{text}', "fr": 'Rappel pour {when} : {text}'},
    "reminder_cancelled": {"es": 'Recordatorio cancelado: {text}', "en": 'Reminder cancelled: {text}', "pt": 'Lembrete cancelado: {text}', "zh": '提醒已取消：{text}', "fr": 'Rappel annulé : {text}'},
    "fact": {"es": 'Recordaré: {fact}', "en": 'Remembered: {fact}', "pt": 'Vou lembrar: {fact}', "zh": '已记住：{fact}', "fr": 'Mémorisé : {fact}'},
    "demo": {"es": 'Negocio de ejemplo cargado', "en": 'Sample business loaded', "pt": 'Negócio de exemplo carregado', "zh": '示例商家已加载', "fr": "Commerce d'exemple chargé"},
    "confirmation_denied": {"es": 'El dueño rechazó una acción', "en": 'The owner declined an action', "pt": 'O dono recusou uma ação', "zh": '店主拒绝了一个操作', "fr": 'Le propriétaire a refusé une action'},
    "confirmation_failed": {"es": 'Una acción confirmada falló: {error}', "en": 'A confirmed action failed: {error}', "pt": 'Uma ação confirmada falhou: {error}', "zh": '一个已确认的操作失败：{error}', "fr": 'Une action confirmée a échoué : {error}'},
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
    text = T[key].get(lang) or T[key]["en"]
    return text.format(**params) if params else text


def fmt_money(amount: float, currency: str, lang: str = "en") -> str:
    text = f"{amount:,.2f}"
    if lang in ("es", "pt", "fr"):  # 1.234,56 (fr uses a space for thousands)
        text = text.replace(",", "_").replace(".", ",").replace("_", " " if lang == "fr" else ".")
    return f"{text} {currency}"


def fmt_date(d: date, lang: str) -> str:
    day = _DAYS.get(lang, _DAYS["en"])[d.weekday()]
    if lang == "zh":
        return f"{d.month}月{d.day}日（{day}）"
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
oui ouais d'accord confirme confirmé parfait exactement vas-y allez-y
""".split())
_FILLER_WORDS = set("""
bueno pues que por favor envíala envíalo envíale envía envia envíalos mándala mándalo manda mandala mándale págale pagale paga págalo pagalo
eso así de acuerdo gracias destiny hazlo porfa
please send it pay them that's thats fine thanks great go do destiny now
favor envia envie manda mande paga pague enviar pagar obrigado obrigada agora
s'il te plaît vous plaît plait envoie envoyez paie payez la le merci
""".split())
_NO_WORDS = set("""
no nop nope espera espérate espere todavía detente nunca alto cancela cancelar cancélala cancélalo cancelalo cancelala anula
not don't dont wait stop never hold cancel cancelled
não nao pare ainda nunca cancele cancelar
non attends attendez arrête jamais pas annule annuler
""".split())
_CANCEL_WORDS = {"cancela", "cancelar", "cancélala", "cancélalo", "cancelalo", "cancelala", "anula", "cancel", "cancele", "annule", "annuler"}
_ZH_YES = ("确认", "没问题", "是的", "好的", "可以", "发送", "发吧", "付款", "支付", "是", "好", "对", "行")
_ZH_FILLER = ("请", "吧", "嗯", "啊", "了", "呢", "发", "付", "就", "这样", "谢谢")
_ZH_NO = ("不", "别", "等", "取消", "停", "没")


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'’-]+", text.lower().replace("’", "'"))


def classify_reply(text: str, action: str = "") -> str:
    """'yes', 'no' or 'other' for a reply to a pending confirmation of `action`."""
    raw = text.strip().lower()
    if not raw or "?" in raw or "¿" in raw or "？" in raw or any(ch.isdigit() for ch in raw):
        return "other"
    if re.search(r"[\u4e00-\u9fff]", raw):
        core = re.sub(r"[\s，。！、,.!]+", "", raw)
        if action == "cancel_invoice":
            core = core.replace("取消", "")
        if any(w in core for w in _ZH_NO) and not core.startswith("没问题"):
            return "no"
        rest, found = core, False
        for w in _ZH_YES + _ZH_FILLER:
            if w in rest:
                found = found or w in _ZH_YES
                rest = rest.replace(w, "")
        return "yes" if found and not rest and len(core) <= 12 else "other"
    words = _words(raw)
    if not words:
        return "other"
    negatives = _NO_WORDS - (_CANCEL_WORDS if action == "cancel_invoice" else set())
    if set(words) & negatives:
        return "no"
    allowed = _YES_WORDS | _FILLER_WORDS | (_CANCEL_WORDS if action == "cancel_invoice" else set())
    if len(words) <= 8 and all(w in allowed for w in words) and any(w in _YES_WORDS for w in words):
        return "yes"
    return "other"
