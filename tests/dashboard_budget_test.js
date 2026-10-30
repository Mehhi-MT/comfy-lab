// index.html の script を最小の DOM 代用で実行し、予算超過・稼働中見込み・ストレージ注記の表示文を確かめる
const fs=require("fs");const html=fs.readFileSync(process.argv[1],"utf8");
const code=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join("\n");
const els={};function el(id){return els[id]||(els[id]={id,style:{},textContent:"",innerHTML:"",dataset:{},classList:{s:new Set(),toggle(c,on){on?this.s.add(c):this.s.delete(c)},add(c){this.s.add(c)},remove(c){this.s.delete(c)},contains(c){return this.s.has(c)}},addEventListener(){} ,querySelectorAll(){return[]}})}
global.document={getElementById:el,addEventListener(){},querySelectorAll(){return[]},querySelector(){return null}};
global.window=global;global.fetch=async()=>({ok:false,text:async()=>"{}",json:async()=>({})});
global.setInterval=()=>0;global.setTimeout=()=>0;global.navigator={clipboard:{writeText(){}}};
eval(code);
const base={model:{file:"m",name:"m",desc:""},vae:{file:"v",name:"v",desc:""},loras:[],settings:{},volume:{total_gb:10,free_gb:5},storage:{note:"ボリューム月額は含まない"}};
render(Object.assign({},base,{budget:{total_usd:0.5,used_usd:0.62,running:{}}}));
const t1=el("bl").textContent;console.log("overrun:",t1);
if(!t1.includes("超過 $0.120"))throw new Error("超過が表示されない");
if(!el("bl").classList.contains("low"))throw new Error("超過の強調なし");
if(el("bbar").style.width!=="100%")throw new Error("バーが100%でない");
const now=Date.now()/1000;
render(Object.assign({},base,{budget:{total_usd:1,used_usd:0.2,running:{podA:{cost_per_hr:0.6,started:now-1800,deadline:now+1800}}}}));
console.log("recorded:",el("bl").textContent);console.log("running:",el("brun").textContent);console.log("storage:",el("bstore").textContent);
if(!/≈ \$0\.30/.test(el("brun").textContent))throw new Error("稼働中の見込みが違う");
if(!el("bl").textContent.includes("残り $0.800"))throw new Error("記録済みの残りが違う");
if(!el("bstore").textContent.includes("含まない"))throw new Error("ストレージ注記なし");
