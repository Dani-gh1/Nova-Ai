// Delingsbillede (1200x630) til assets/og-image.png. Brug: NODE_PATH=... node tools/og.js
const fs = require("fs"), path = require("path");
const { chromium } = require("playwright");
const logo = fs.readFileSync(path.join(__dirname, "..", "hub", "static", "logo.svg"), "utf8");
const html = `<style>html,body{margin:0}body{width:1200px;height:630px;display:flex;align-items:center;gap:56px;padding:0 90px;box-sizing:border-box;
background:radial-gradient(900px 500px at 85% 10%,#a78bfa55,transparent),linear-gradient(135deg,#0b0e14,#151a2b);color:#eef1f7;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
svg{width:300px;height:300px;flex:none;filter:drop-shadow(0 30px 50px rgba(91,91,240,.55))}h1{font-size:84px;line-height:1.02;letter-spacing:-.035em;margin:0 0 22px}
h1 em{font-style:normal;background:linear-gradient(120deg,#8b8bff,#c4b5fd);-webkit-background-clip:text;color:transparent}p{font-size:32px;color:#a7b0c0;margin:0;line-height:1.35}
.b{display:inline-block;margin-top:30px;font-size:22px;font-weight:600;color:#e5b45a;background:#e5b45a22;border-radius:99px;padding:6px 18px}</style>
${logo}<div><h1>Nova AI<br><em>Alle dine botter.</em></h1><p>Strategier, regler, desk og handler ét sted.</p><span class="b">Kun papirhandel · fiktive penge</span></div>`;
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
  await p.setContent(html); await p.screenshot({ path: path.join(__dirname, "..", "assets", "og-image.png") }); await b.close(); console.log("og-image skrevet");
})();
