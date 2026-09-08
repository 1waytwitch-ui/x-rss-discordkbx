# x-rss-discord

Relais gratuit : flux RSS [@kbourcrypto](https://x.com/kbourcrypto) → webhook Discord, via GitHub Actions (toutes les 10 minutes).

## Setup (2 minutes)

1. Repo **Settings → Secrets and variables → Actions → New repository secret**
   - `RSS_URL` = `https://rss.app/feeds/cxz7oMO1M5e1leXS.xml`
   - `DISCORD_WEBHOOK_URL` = ton URL Discord (`https://discord.com/api/webhooks/...`)
2. Settings → Actions → General → Workflow permissions → **Read and write** (pour que `seen.json` soit mis à jour).
3. Onglet **Actions** → workflow **RSS to Discord** → **Run workflow**.

Le premier run mémorise les posts existants sans les renvoyer. Les suivants n'envoient que les nouveaux.
