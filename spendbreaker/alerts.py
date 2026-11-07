"""Alert sinks: stdout, Telegram bot, ntfy. Credentials from env vars only."""
import sys

PRIORITY = {"info": "3", "warn": "4", "critical": "5"}
EMOJI_FREE_PREFIX = {"info": "[info]", "warn": "[WARN]", "critical": "[CRITICAL]"}


class AlertError(Exception):
    pass


def format_event(ev):
    return f"{EMOJI_FREE_PREFIX.get(ev.severity, '[WARN]')} spendbreaker: {ev.message}"


class StdoutSink:
    name = "stdout"

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def send(self, ev):
        print(format_event(ev), file=self.stream)


class TelegramSink:
    name = "telegram"

    def __init__(self, cfg, http, env):
        self.http = http
        tok_var = cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
        chat_var = cfg.get("chat_id_env", "TELEGRAM_CHAT_ID")
        self.token, self.chat = env.get(tok_var, ""), env.get(chat_var, "")
        if not self.token or not self.chat:
            raise AlertError(f"telegram: set {tok_var} and {chat_var}")

    def send(self, ev):
        self.http.request("POST", f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json_body={"chat_id": self.chat, "text": format_event(ev),
                                     "disable_web_page_preview": True})


class NtfySink:
    name = "ntfy"

    def __init__(self, cfg, http, env):
        self.http = http
        self.server = cfg.get("server", "https://ntfy.sh").rstrip("/")
        topic_var = cfg.get("topic_env", "NTFY_TOPIC")
        self.topic = env.get(topic_var, "")
        if not self.topic:
            raise AlertError(f"ntfy: set {topic_var} (treat the topic name like a password)")
        self.auth = env.get(cfg.get("access_token_env", "NTFY_ACCESS_TOKEN"), "")

    def send(self, ev):
        h = {"Title": f"spendbreaker: {ev.provider} {ev.kind}", "Priority": PRIORITY.get(ev.severity, "4"),
             "Tags": "money_with_wings" if ev.severity != "info" else "chart_with_upwards_trend"}
        if self.auth:
            h["Authorization"] = f"Bearer {self.auth}"
        self.http.request("POST", f"{self.server}/{self.topic}", headers=h, data=ev.message.encode())


def build_sinks(acfg, http, env, stream=None):
    sinks, errors = [], []
    if acfg.get("stdout", True):
        sinks.append(StdoutSink(stream))
    for key, cls in (("telegram", TelegramSink), ("ntfy", NtfySink)):
        c = acfg.get(key)
        if c is not None and c is not False:  # `telegram: {}` means "enabled with defaults"
            try:
                sinks.append(cls(c if isinstance(c, dict) else {}, http, env))
            except AlertError as e:
                errors.append(str(e))
    return sinks, errors
