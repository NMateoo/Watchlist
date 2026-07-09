# 📈 Watchlist

Monitoriza tus acciones, ETFs y cripto desde una web y recibe alertas en Telegram:

- ⚡ **Precios en vivo** — el dashboard se actualiza solo cada pocos segundos (intervalo configurable en Ajustes, mínimo 5 s).
- 🔎 **Buscador con sugerencias** — escribe "apple" o "santander" y elige el ticker.
- 🔔 **Alertas de precio** — te avisa cuando un valor cruza un umbral que definas.
- ⚡ **Cambios bruscos** — aviso si algo se mueve más de X% en el día (una vez al día por valor).
- 📊 **Resumen diario** — el estado de toda tu watchlist a la hora que elijas.
- 📝 **Notas** — precio objetivo y apuntes personales por cada valor.

Datos de [Yahoo Finance](https://finance.yahoo.com) (gratis, sin API key). Funciona con
acciones de EE.UU. (`AAPL`), europeas (`SAN.MC`, `ITX.MC`, `AIR.PA`), cripto (`BTC-USD`)
y ETFs (`SPY`).

## Ejecutar en local

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # y rellena tus claves de Telegram
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Abre http://localhost:8000

## Configurar el bot de Telegram

1. En Telegram, habla con **@BotFather** → `/newbot` → elige nombre. Te dará un **token**.
2. Ponlo en `.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`
3. Abre un chat con tu bot y mándale cualquier mensaje ("hola").
4. Arranca la app y entra en **Ajustes** (`/settings`): te mostrará tu `chat_id`.
5. Añade `TELEGRAM_CHAT_ID=...` al `.env`, reinicia y pulsa "Enviar mensaje de prueba".

## Desplegar en Render

1. Sube este repo a GitHub.
2. **Base de datos** (recomendado): crea una gratis en [Neon](https://neon.tech) y copia su
   *connection string*. En el plan gratuito de Render el disco es efímero: sin esto, la
   watchlist se borra en cada deploy.
3. En [Render](https://render.com): **New → Blueprint**, conecta el repo (detecta `render.yaml`).
4. Rellena las variables `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` y `DATABASE_URL` (la de Neon).
5. **Importante (plan gratuito):** Render "duerme" el servicio tras 15 min sin visitas y las
   alertas dejarían de comprobarse. Solución: crea un monitor gratuito en
   [UptimeRobot](https://uptimerobot.com) que haga ping a `https://tu-app.onrender.com/health`
   cada 5 minutos.

## Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token de @BotFather |
| `TELEGRAM_CHAT_ID` | — | Tu chat con el bot |
| `CHECK_INTERVAL_MINUTES` | `10` | Frecuencia de comprobación de alertas |
| `DAILY_SUMMARY_TIME` | `22:10` | Hora del resumen diario (HH:MM) |
| `TIMEZONE` | `Europe/Madrid` | Zona horaria |
| `MOVE_ALERT_THRESHOLD` | `5` | % de cambio diario que dispara aviso (editable en Ajustes) |
| `DATABASE_URL` | SQLite local | En producción, Postgres (Neon) |
