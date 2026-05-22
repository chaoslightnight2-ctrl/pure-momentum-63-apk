package com.quant.paperbot;

import org.json.JSONArray;
import org.json.JSONObject;

public class DashboardRenderer {
    public static String render(String status, String message, String ordersJson, String statsJson) {
        String safeStatus = esc(status);
        String safeMessage = esc(message);
        JSONArray orders;
        JSONObject stats;
        try { orders = new JSONArray(ordersJson == null ? "[]" : ordersJson); } catch (Exception e) { orders = new JSONArray(); }
        try { stats = new JSONObject(statsJson == null ? "{}" : statsJson); } catch (Exception e) { stats = new JSONObject(); }
        StringBuilder rows = new StringBuilder();
        StringBuilder positionRows = new StringBuilder();
        double buyTotal = 0.0;
        double sellTotal = 0.0;
        for (int i = 0; i < orders.length(); i++) {
            JSONObject o = orders.optJSONObject(i);
            if (o == null) continue;
            String side = o.optString("side");
            double notional = orderNotional(o);
            if ("buy".equalsIgnoreCase(side)) buyTotal += notional;
            if ("sell".equalsIgnoreCase(side)) sellTotal += notional;
            rows.append("<tr><td><b>").append(esc(o.optString("symbol"))).append("</b></td><td>")
                .append(esc(sideText(side))).append("</td><td class='num'>")
                .append(esc(orderQty(o))).append("</td><td class='num'>")
                .append(money(notional)).append("</td><td>")
                .append(esc(stateText(o.optString("state", o.optString("status", "planlandi"))))).append("</td></tr>");
        }
        if (rows.length() == 0) rows.append("<tr><td colspan='5'>Aktif emir farki yok.</td></tr>");
        JSONArray positions = stats.optJSONArray("positions");
        if (positions != null) {
            for (int i = 0; i < positions.length(); i++) {
                JSONObject p = positions.optJSONObject(i);
                if (p == null) continue;
                double pl = parseDouble(p.optString("unrealized_pl", "0"));
                positionRows.append("<tr><td><b>").append(esc(p.optString("symbol"))).append("</b></td><td class='num'>")
                    .append(esc(p.optString("qty"))).append("</td><td class='num'>")
                    .append(money(parseDouble(p.optString("market_value", "0")))).append("</td><td class='num ")
                    .append(pl >= 0 ? "good" : "bad").append("'>").append(signedMoney(pl)).append("</td></tr>");
            }
        }
        if (positionRows.length() == 0) positionRows.append("<tr><td colspan='4'>Acik pozisyon yok.</td></tr>");
        JSONObject history = stats.optJSONObject("portfolio_history");
        String equityData = jsNumberArray(history == null ? null : history.optJSONArray("equity"));
        String runningText = stats.optBoolean("running", false) ? "Calisiyor" : "Durdu";
        String runningClass = stats.optBoolean("running", false) ? "good" : "bad";
        return "<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'>" +
            "<meta http-equiv='refresh' content='30'><style>" +
            "body{font-family:sans-serif;background:#0f172a;color:#f8fafc;margin:0;padding:16px}h1{font-size:24px;margin:0}.sub{color:#cbd5e1;margin:6px 0 16px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.card{background:#111827;border:1px solid #243244;border-radius:8px;padding:13px}.label{color:#94a3b8;font-size:12px}.value{font-size:22px;font-weight:800;margin-top:4px}.good{color:#34d399}.bad{color:#f87171}.warn{color:#fbbf24}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #243244;padding:9px 4px;text-align:left}th{font-size:12px;color:#94a3b8}.num{text-align:right}.full{grid-column:1/3}.hero{background:#162033;border:1px solid #334155;border-radius:8px;padding:14px;margin-bottom:12px}.pill{display:inline-block;padding:5px 9px;border-radius:999px;background:#1f2937;color:#93c5fd;font-size:12px;font-weight:700;margin-top:8px}.seg button{background:#1f2937;color:#cbd5e1;border:1px solid #334155;border-radius:7px;padding:7px 10px;margin-right:6px}.seg button.on{background:#2563eb;color:white}canvas{width:100%;height:190px}@media(max-width:560px){.grid{grid-template-columns:1fr}.full{grid-column:1}}" +
            "</style></head><body><div class='hero'><h1>Pure Momentum 63</h1><div class='sub'>63 gun momentum, top 3 secim, toplam 2x hedef exposure.</div><span class='pill'>" + safeStatus + "</span></div><div class='grid'>" +
            card("Bot durumu", runningText, runningClass) +
            card("Son mesaj", safeMessage, "") +
            card("Portfoy", money(stats.optDouble("equity", 0)), "good") +
            card("Nakit", money(stats.optDouble("cash", 0)), "good") +
            card("Alim gucu", money(stats.optDouble("buying_power", 0)), "good") +
            card("Bugun", signedMoney(stats.optDouble("daily_pl", 0)), stats.optDouble("daily_pl", 0) >= 0 ? "good" : "bad") +
            card("Hafta", signedMoney(stats.optDouble("weekly_pl", 0)), stats.optDouble("weekly_pl", 0) >= 0 ? "good" : "bad") +
            card("Ay", signedMoney(stats.optDouble("monthly_pl", 0)), stats.optDouble("monthly_pl", 0) >= 0 ? "good" : "bad") +
            card("Alinacak", money(buyTotal), "warn") +
            card("Satilacak", money(sellTotal), "warn") +
            card("Dongu", esc(stats.optString("loop", "15 dk")), "") +
            "<div class='card full'><div class='label'>Portfoy grafigi</div><div class='seg'><button id='d' onclick='draw(1)'>Gun</button><button id='w' onclick='draw(5)'>Hafta</button><button id='m' onclick='draw(21)' class='on'>Ay</button></div><canvas id='chart'></canvas></div>" +
            "<div class='card full'><div class='label'>Son 5 emir / guncel emir plani</div><table><thead><tr><th>Sembol</th><th>Yon</th><th class='num'>Lot</th><th class='num'>Tutar</th><th>Durum</th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
            "<div class='card full'><div class='label'>Bulunan pozisyonlar ve kar/zarar</div><table><thead><tr><th>Hisse</th><th class='num'>Lot</th><th class='num'>Deger</th><th class='num'>K/Z</th></tr></thead><tbody>" + positionRows + "</tbody></table></div>" +
            "<div class='card full'><div class='label'>Strateji ve risk kontrolleri</div><p>Pure momentum: TQQQ, TECL, SOXL, UPRO, SPXL, MSTR, COIN, MARA, RIOT, NVDA, AMD, PLTR, SMCI, TSLA, CVNA, APP, HOOD evreninde son 63 gun getirisi en guclu 3 sembol secilir. Hedef gross exposure toplam 2x; Alpaca alim gucu ana paranin yaklasik 2 kati olarak %100 kullanilir, piyasa saati, acik emir varsa tekrar gondermeme, tam lot emir, API throttle ve retry aktif.</p></div>" +
            "</div><script>var eq=" + equityData + ",current=" + stats.optDouble("equity", 0) + ";function usd(v){return Number(v||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}function draw(n){['d','w','m'].forEach(function(id){document.getElementById(id).className=''});document.getElementById(n==1?'d':n==5?'w':'m').className='on';var c=document.getElementById('chart'),x=c.getContext('2d'),w=c.width=c.offsetWidth*2,h=c.height=190*2;x.clearRect(0,0,w,h);var a=eq.slice(Math.max(0,eq.length-n-1));if(a.length<2){x.fillStyle='#94a3b8';x.font='28px sans-serif';x.fillText('Grafik icin veri bekleniyor',24,90);return;}var mn=Math.min.apply(null,a),mx=Math.max.apply(null,a),pad=28,span=mx-mn||1;x.strokeStyle='#334155';x.lineWidth=2;x.beginPath();x.moveTo(pad,h-pad);x.lineTo(w-pad,h-pad);x.stroke();x.strokeStyle='#60a5fa';x.lineWidth=5;x.beginPath();a.forEach(function(v,i){var px=pad+i*(w-2*pad)/(a.length-1),py=h-pad-((v-mn)/span)*(h-2*pad);if(i)x.lineTo(px,py);else x.moveTo(px,py);});x.stroke();x.fillStyle='#f8fafc';x.font='30px sans-serif';x.fillText('Portfoy '+usd(current||a[a.length-1]),pad,34);x.fillStyle=a[a.length-1]>=a[0]?'#34d399':'#f87171';x.font='24px sans-serif';x.fillText((a[a.length-1]-a[0]>=0?'+':'')+usd(a[a.length-1]-a[0]),pad,66);}draw(21);</script></body></html>";
    }
    private static String card(String label, String value, String cls) { return "<div class='card'><div class='label'>" + label + "</div><div class='value " + cls + "'>" + value + "</div></div>"; }
    private static String money(double value) { return String.format(java.util.Locale.US, "%,.2f", value); }
    private static String signedMoney(double value) { return (value >= 0 ? "+" : "") + money(value); }
    private static String sideText(String side) { return "buy".equalsIgnoreCase(side) ? "AL" : "sell".equalsIgnoreCase(side) ? "SAT" : side; }
    private static String stateText(String state) {
        if (state == null) return "";
        if ("filled".equalsIgnoreCase(state)) return "doldu";
        if ("new".equalsIgnoreCase(state)) return "acik";
        if ("canceled".equalsIgnoreCase(state)) return "iptal";
        if ("planned".equalsIgnoreCase(state) || "planlandi".equalsIgnoreCase(state)) return "planlandi";
        return state;
    }
    private static String orderQty(JSONObject o) {
        String qty = o.optString("qty", "");
        if (qty.isEmpty()) qty = o.optString("filled_qty", "");
        return qty;
    }
    private static double orderNotional(JSONObject o) {
        double notional = o.optDouble("notional", 0.0);
        if (notional > 0.0) return notional;
        double qty = parseDouble(o.optString("filled_qty", o.optString("qty", "0")));
        double price = o.optDouble("filled_avg_price", 0.0);
        return qty * price;
    }
    private static double parseDouble(String value) {
        try { return Double.parseDouble(value); } catch (Exception e) { return 0.0; }
    }
    private static String jsNumberArray(JSONArray arr) {
        if (arr == null) return "[]";
        StringBuilder out = new StringBuilder("[");
        int count = 0;
        for (int i = 0; i < arr.length(); i++) {
            double value = parseDouble(arr.optString(i, "0"));
            if (value <= 1.0) continue;
            if (count > 0) out.append(",");
            out.append(value);
            count++;
        }
        return out.append("]").toString();
    }
    private static String esc(String s) { return s == null ? "" : s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;"); }
}
