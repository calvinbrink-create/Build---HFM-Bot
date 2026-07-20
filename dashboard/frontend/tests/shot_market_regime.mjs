import fs from "node:fs";
import { chromium } from "playwright";
const ENV_FILE="/etc/scalpbot/scalpbot-mt5-dashboard.env", API="http://127.0.0.1:8010/api";
const URL="https://mytradebot.co.za/dashboard/mt5/";
function env(f){const v={};for(const l of fs.readFileSync(f,"utf8").split(/\r?\n/)){const t=l.trim();if(!t||t.startsWith("#")||!t.includes("="))continue;const i=t.indexOf("=");v[t.slice(0,i)]=t.slice(i+1).replace(/^['"]|['"]$/g,"");}return v;}
const e=env(ENV_FILE);
const tok=(await(await fetch(`${API}/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:e.MT5_LOGIN,password:e.MT5_PASSWORD})})).json()).token;
const b=await chromium.launch({headless:true});
const c=await b.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1.5,ignoreHTTPSErrors:true});
await c.addInitScript(({t})=>{localStorage.setItem("cipherfx_mt5_token",t);localStorage.setItem("cipherfx_dashboard_auth_v1",JSON.stringify({email:"r@vps",expiresAt:Date.now()+3600000,dashboards:["mt5"]}));},{t:tok});
const p=await c.newPage();
await p.goto(`${URL}?r=${Date.now()}`,{waitUntil:"networkidle",timeout:30000});
await p.waitForTimeout(1200);
await p.locator(".bottom-nav button").filter({has:p.locator('.desktop-label:text-is("Market")')}).first().click();
await p.waitForTimeout(2500);
await p.screenshot({path:"/tmp/claude-0/-root/9dca97ce-58d8-4690-a34d-0295150b0558/scratchpad/market-regime.png"});
await b.close();
console.log("done");
