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
    """Offline Stack-style game: tap to drop and trim the moving block."""
    return '''<style>
body{font-family:sans-serif;margin:0;padding:8px;background:#171638;color:#fff;text-align:center;overflow:hidden}
.head{font-size:34px;font-weight:800;line-height:38px}.hint{font-size:12px;opacity:.72;margin:3px 0 7px}
#scene{height:232px;position:relative;margin:auto;max-width:280px;overflow:hidden;perspective:500px}
#tower{position:absolute;inset:0}.block{position:absolute;height:16px;border-radius:2px;background:linear-gradient(90deg,#dd3e77,#f36c91);box-shadow:0 4px 0 #9b2759,0 9px 14px #090825;transition:left .08s linear}
.block:before{content:'';position:absolute;left:7px;right:-7px;top:-7px;height:7px;background:#f17198;transform:skewX(45deg);transform-origin:left bottom}
.block:after{content:'';position:absolute;right:-7px;top:0;width:7px;height:16px;background:#8d235f;transform:skewY(45deg);transform-origin:left top}
#tap{position:absolute;inset:0;border:0;background:transparent;color:#fff;font-size:18px;font-weight:700;text-shadow:0 2px 3px #090825}
#over{position:absolute;inset:65px 18px auto;padding:14px;border-radius:10px;background:#211e51dd;font-size:16px;display:none}
</style><div class="head" id="score">0</div><div class="hint">Tap to drop · Best <span id="best">0</span></div>
<div id="scene"><div id="tower"></div><button id="tap">TAP TO DROP</button><div id="over">Башня готова.<br><small>Нажмите, чтобы начать заново</small></div></div>
<script>
const scene=document.getElementById('scene'),tower=document.getElementById('tower'),tap=document.getElementById('tap'),score=document.getElementById('score'),bestEl=document.getElementById('best'),over=document.getElementById('over');
let blocks=[],moving=null,level=0,dir=1,step=2.1,best=0,timer=null,dead=false;
function block(x,w,i){const e=document.createElement('div');e.className='block';e.style.left=x+'px';e.style.width=w+'px';e.style.bottom=(10+i*18)+'px';tower.appendChild(e);return e}
function start(){tower.innerHTML='';blocks=[];level=0;dead=false;over.style.display='none';tap.textContent='TAP TO DROP';score.textContent=0;const base={x:78,w:124,el:block(78,124,0)};blocks.push(base);spawn()}
function spawn(){const p=blocks[blocks.length-1];moving={x:dir>0?8:scene.clientWidth-p.w-8,w:p.w,el:block(8,p.w,level+1)};moving.el.style.bottom=(10+(level+1)*18)+'px';timer=setInterval(tick,28)}
function tick(){if(!moving)return;moving.x+=dir*step;if(moving.x<4||moving.x+moving.w>scene.clientWidth-4){dir*=-1;moving.x+=dir*step}moving.el.style.left=moving.x+'px'}
function drop(){if(dead){start();return}clearInterval(timer);const p=blocks[blocks.length-1],left=Math.max(p.x,moving.x),right=Math.min(p.x+p.w,moving.x+moving.w),w=right-left;if(w<7){dead=true;moving.el.style.opacity='.35';over.style.display='block';tap.textContent='RESTART';return}moving.x=left;moving.w=w;moving.el.style.left=left+'px';moving.el.style.width=w+'px';blocks.push(moving);level++;score.textContent=level;best=Math.max(best,level);bestEl.textContent=best;dir*=-1;step=Math.min(4.4,step+.12);spawn()}
tap.onclick=drop;start();
</script>'''

