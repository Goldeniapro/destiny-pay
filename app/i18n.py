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


def t(key: str, lang: str, **params) -> str:
    text = T[key].get(lang) or T[key]["en"]
    return text.format(**params) if params else text


def fmt_money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def fmt_date(d: date, lang: str) -> str:
    day = _DAYS.get(lang, _DAYS["en"])[d.weekday()]
    if lang == "zh":
        return f"{d.month}月{d.day}日（{day}）"
    if lang == "en":
        return f"{day} {d.month}/{d.day}"
    return f"{day} {d.day}/{d.month}"


# --- yes / no, decided by code ------------------------------------------------------
# A spoken "yes" executes a money action only when it is short, clearly affirmative, has no
# negation and no change request ("sí, pero cambia el monto" must go back to the agent).
_YES = {
    "es": "sí si dale confirmo confirmado confirma envíala envíalo envíale envía envia mándala mándalo manda adelante hazlo hágalo correcto ok okay listo claro perfecto paga págale pagale procede cancélala cancélalo cancela",
    "en": "yes yeah yep yup sure confirm confirmed send go ahead do it ok okay correct right perfect pay proceed cancel it",
    "pt": "sim pode confirmo confirmado confirma envia envie manda mande ok certo claro perfeito paga pague prossiga cancela",
    "fr": "oui ouais d'accord confirme confirmé envoie envoyez vas-y allez-y ok parfait correct paie payez annule",
}
_NO = {
    "es": "no nop espera espérate todavía detente nunca",
    "en": "no nope wait stop don't dont not hold never",
    "pt": "não nao espera pare ainda nunca",
    "fr": "non attends attendez arrête pas jamais",
}
_CHANGE = {"pero", "cambia", "cambiar", "corrige", "corregir", "mejor", "en vez", "but", "change", "instead", "fix",
           "mas", "mude", "troque", "corrija", "mais", "plutôt",
           # timing changes go back to the agent too
           "mañana", "luego", "después", "later", "tomorrow", "amanhã", "depois", "demain", "tard"}
_ZH_YES = ("是", "好", "确认", "可以", "发送", "发吧", "对", "没问题", "行", "付款", "支付", "取消吧")
_ZH_NO = ("不", "别", "等", "取消", "停")


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'’-]+", text.lower())


def classify_reply(text: str) -> str:
    """'yes', 'no' or 'other' for a reply to a pending confirmation."""
    raw = text.strip().lower()
    if not raw:
        return "other"
    if any(ch.isdigit() for ch in raw):
        return "other"  # numbers mean a correction ("sí, a 40 dólares")
    if re.search(r"[一-鿿]", raw):
        if any(w in raw for w in ("但是", "改", "换", "换成")):
            return "other"
        no = any(w in raw for w in _ZH_NO) and not raw.startswith("取消吧")
        if no and not any(raw.startswith(y) for y in ("是", "好", "确认")):
            return "no"
        if len(raw) <= 12 and any(w in raw for w in _ZH_YES):
            return "yes"
        return "other"
    words = _words(raw)
    joined = " " + " ".join(words) + " "
    no_words = {w for v in _NO.values() for w in v.split()}
    # a "no" anywhere wins: cancelling is always safe, nothing moves
    if words and (words[0] in no_words or (set(words) & {"no", "não", "nao", "non", "nope"})):
        return "no"
    if set(words) & (no_words - {"pas", "ainda", "todavía"}):
        return "no"
    if any(f" {c} " in joined for c in _CHANGE):
        return "other"
    yes_phrases = [p for v in _YES.values() for p in v.split()] + ["go ahead", "do it", "vas-y", "allez-y", "d'accord"]
    if len(words) <= 8 and any(f" {p} " in joined for p in yes_phrases):
        return "yes"
    return "other"
