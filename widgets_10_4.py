"""Inline HTML widgets used by the private-chat Bot API 10.4 commands."""

import json


def _json(value) -> str:
    # Embed an object directly in the script.  HTML entities such as &quot; are
    # not decoded inside script blocks and would make JSON.parse() fail.
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def dossier_widget(profile: dict) -> str:
    """A self-contained protected personal-card widget.

    Widgets are offline by design, so the current card is embedded in chat_data
    by the bot when the message is sent.  No private data leaves Telegram.
    """
    data = _json(profile)
    return f'''<style>
body{{font-family:sans-serif;margin:0;padding:12px;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color)}}
.card{{border:1px solid var(--tg-theme-hint-color);border-radius:12px;padding:12px}}
h3{{margin:0 0 10px;color:var(--tg-theme-link-color)}}
.row{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--tg-theme-secondary-bg-color)}}
.k{{opacity:.65}}
</style><div class="card"><h3>🛡 Личное дело</h3><div id="v"></div><button id="wallet">💎 Показать TON-кошелёк</button><div id="walletResult"></div></div>
<script>
const p={data};
const rows=[['Ник',p.nick],['Позывной',p.callsign],['Ранг',p.rank_name],['Отдел',p.department],['Жетон',p.token],['Предупреждения',p.warns]];
document.getElementById('v').innerHTML=rows.map(x=>'<div class="row"><span class="k">'+x[0]+'</span><b>'+x[1]+'</b></div>').join('');
document.getElementById('wallet').onclick=()=>TelegramWidget.request('wallet',{{wallet:true}}).then(x=>document.getElementById('walletResult').textContent=x.result||'Кошелёк не подключён').catch(()=>document.getElementById('walletResult').textContent='Кошелёк недоступен');
</script>'''


def tower_widget() -> str:
    """Offline one-player tower mini-game for private chats."""
    return '''<style>
body{font-family:sans-serif;margin:0;padding:10px;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color);text-align:center}
#tower{height:210px;display:flex;flex-direction:column-reverse;align-items:center;justify-content:flex-start;padding-bottom:8px}
.block{width:70%;height:18px;margin:2px;border-radius:4px;background:var(--tg-theme-button-color)}
button{border:0;border-radius:8px;padding:10px 18px;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);font-weight:700}
.muted{opacity:.7;font-size:12px}
</style><b>🏗 Башня</b><div id="tower"></div><button id="build">Поставить блок</button><div class="muted" id="score">Высота: 0</div>
<script>
let n=0;const t=document.getElementById('tower'),s=document.getElementById('score');
document.getElementById('build').onclick=()=>{n++;const b=document.createElement('div');b.className='block';b.style.width=(55+Math.random()*25)+'%';t.appendChild(b);s.textContent='Высота: '+n;if(n>=10){document.getElementById('build').disabled=true;s.textContent='🏆 Башня построена: '+n;}};
</script>'''

