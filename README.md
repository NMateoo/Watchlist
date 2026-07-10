# 📈 Watchlist

Monitoriza tus acciones, ETFs y cripto desde una web y recibe alertas en Telegram:

- ⚡ **Precios en vivo** — el dashboard se actualiza solo cada pocos segundos (configurable en Ajustes, mínimo 5 s), incluyendo pre-market y after-hours, con indicador del estado del mercado.
- 🗂️ **Varias listas de seguimiento** — organiza tus valores en pestañas; créalas, renómbralas
  y elimínalas desde la web o el bot.
- 🔎 **Buscador con sugerencias** — escribe "apple", "santander", "oro" o "eurusd" y elige el ticker. Incluye materias primas: metales spot (`XAUUSD`, `XAGUSD`, `XPTUSD`, `XPDUSD`, vía gold-api.com) y futuros (`GC=F`, `CL=F`).
- 🔔 **Alertas de precio** — te avisa cuando un valor cruza un umbral: un precio (`150.50`) o un
  % desde el precio actual (`5%`). Las alertas 🔁 se re-arman solas al re-cruzar el umbral.
- ⚡ **Cambios bruscos** — aviso si algo se mueve más de X% en el día (una vez al día por valor).
- 📊 **Resumen diario** — el estado de toda tu watchlist a la hora que elijas.
- 📝 **Notas** — precio objetivo y apuntes personales por cada valor.
- 💼 **Posición** — apunta cantidad y precio de compra y verás tu ganancia/pérdida en vivo en el
  dashboard, la ficha, el bot y el resumen.
- 📋 **Dashboard ordenable** — clic en las cabeceras para ordenar por precio, % del día, distancia
  al objetivo o rentabilidad; mini-gráfica del último mes por valor. Tema claro/oscuro automático.
- 🤖 **Bot interactivo** — manda `/menu` al bot y maneja TODO desde Telegram: listas, añadir/quitar
  valores, alertas, notas, objetivo y ajustes. Comandos: `/menu`, `/precio AAPL`, `/resumen`, `/ayuda`.
  Solo una instancia puede escuchar los comandos a la vez: la que tenga `BOT_POLLING=1` (por defecto);
  pon `BOT_POLLING=0` en las demás (p. ej. en local si ya lo atiende Render).

Datos de [Yahoo Finance](https://finance.yahoo.com) (gratis, sin API key). Funciona con
acciones de EE.UU. (`AAPL`), europeas (`SAN.MC`, `ITX.MC`, `AIR.PA`), cripto (`BTC-USD`)
y ETFs (`SPY`).

## Ejecutar en local

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Para desarrollar, instala también las dependencias de test y ejecútalos con:

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\pytest
```

Crea un archivo `.env` en la raíz con tus claves (ver tabla de variables más abajo):

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=...
DATABASE_URL=postgresql://...   # opcional; sin esto usa SQLite local
```

Abre http://localhost:8000

## Configurar el bot de Telegram

1. En Telegram, habla con **@BotFather** → `/newbot` → elige nombre. Te dará un **token**.
2. Ponlo en `.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`
3. Abre un chat con tu bot y mándale cualquier mensaje ("hola").
4. Arranca la app y entra en **Ajustes** (`/settings`): te mostrará tu `chat_id`.
5. Añade `TELEGRAM_CHAT_ID=...` al `.env`, reinicia y pulsa "Enviar mensaje de prueba".

## Usar Postgres de Neon en vez de SQLite

1. Crea el proyecto en [neon.tech](https://neon.tech) y copia el *connection string*.
2. Ponlo en el `.env`: `DATABASE_URL=postgresql://usuario:contraseña@ep-xxx.neon.tech/neondb?sslmode=require`
3. Reinicia la app: las tablas se crean solas al arrancar.

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
