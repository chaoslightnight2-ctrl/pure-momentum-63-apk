package com.quant.paperbot;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;

public class BotService extends Service {
    private static final String CHANNEL_ID = "quant_bot_running_v3";
    private static final double CAPITAL = 100000.0;
    private static final double GROSS_CAP = 1.80;
    private static final int PURE_MOMENTUM_LOOKBACK_DAYS = 63;
    private static final int PURE_MOMENTUM_TOP_N = 7;
    private static final int PURE_MOMENTUM_REBALANCE_DAYS = 7;
    private static final double PURE_MOMENTUM_TARGET_GROSS = 1.80;
    private static final double NORMAL_MIN_5D_MOMENTUM = 0.0;
    private static final double MID_BREADTH_CASH_LOW = 0.50;
    private static final double MID_BREADTH_CASH_HIGH = 0.66;
    private static final double MID_BREADTH_SLEEVE_SPY20_MIN = 0.03;
    private static final int MID_BREADTH_SLEEVE_TOP_N = 3;
    private static final double MID_BREADTH_SLEEVE_TARGET_GROSS = 0.80;
    private static final double WEAK_BREADTH_THRESHOLD = 0.33;
    private static final double DAILY_BUY_BLOCK_LOSS_PCT = 0.02;
    private static final double SESSION_BUY_BLOCK_DRAWDOWN_PCT = 0.025;
    private static final double MIN_PROTECTIVE_STOP_DISTANCE_PCT = 0.015;
    private static final long LOOP_MS = 15L * 60L * 1000L;
    private static final long CANCEL_WAIT_MS = 12L * 1000L;
    private static final long CANCEL_POLL_MS = 750L;
    private static final double MIN_DELTA_NOTIONAL = 25.0;
    private static final double BUYING_POWER_LIMIT_MULTIPLIER = 1.0;
    private static final String BASE_URL = "https://paper-api.alpaca.markets";
    private static final String DATA_URL = "https://data.alpaca.markets";
    private volatile boolean stopRequested = false;
    private SharedPreferences prefs;
    private long lastRequestAt = 0L;
    private Thread workerThread;

    private static final String[] PURE_MOMENTUM_UNIVERSE = new String[] {
        "TQQQ", "TECL", "SOXL", "UPRO", "SPXL", "MSTR", "COIN", "MARA", "RIOT",
        "NVDA", "AMD", "PLTR", "SMCI", "TSLA", "CVNA", "APP", "HOOD"
    };
    private static final String REGIME_SYMBOL = "SPY";
    private static final String[] LEGACY_MANAGED_SYMBOLS = new String[] {
        "AVGO", "TSLA", "NVDA", "GLD"
    };

    @Override public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("quant_bot", MODE_PRIVATE);
        createChannel();
        startForeground(1, notification("Bot calisiyor - arka planda takip aktif"));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        stopRequested = false;
        if (workerThread == null || !workerThread.isAlive()) {
            workerThread = new Thread(new Runnable() { @Override public void run() { loop(); } }, "quant-bot-loop");
            workerThread.start();
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        stopRequested = true;
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private void loop() {
        while (!stopRequested && prefs.getBoolean("running", true)) {
            try { runOnceWithWakeLock(); }
            catch (Exception e) { saveDashboard("error", e.getMessage(), new JSONArray(), new JSONObject()); }
            sleepInterruptibly(LOOP_MS);
        }
        stopForeground(true);
        stopSelf();
    }

    private void runOnceWithWakeLock() throws Exception {
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        PowerManager.WakeLock wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "QuantPaperBot:RunOnce");
        wakeLock.acquire(5L * 60L * 1000L);
        try {
            runOnce();
        } finally {
            if (wakeLock.isHeld()) wakeLock.release();
        }
    }

    private void runOnce() throws Exception {
        String apiKey = prefs.getString("api_key", "");
        String secret = prefs.getString("secret_key", "");
        if (apiKey.isEmpty() || secret.isEmpty()) {
            saveDashboard("bekliyor", "Alpaca API bilgilerini girip Kaydet ve Baslat'a bas.", new JSONArray(), stats(0, 0, "15 dk"));
            return;
        }
        JSONObject account = api("GET", "/v2/account", null, apiKey, secret);
        double buyingPower = account.optDouble("buying_power", 0.0);
        double cash = account.optDouble("cash", 0.0);
        double equity = account.optDouble("equity", 0.0);
        double lastEquity = account.optDouble("last_equity", 0.0);
        JSONArray recentOrders = recentOrders(apiKey, secret);
        JSONArray positionsJson = apiArray("GET", "/v2/positions", apiKey, secret);
        JSONObject portfolioHistory = portfolioHistory(apiKey, secret);
        JSONObject baseStats = stats(0, buyingPower, cash, equity, lastEquity, "15 dk", recentOrders, positionsJson, portfolioHistory);
        if (!"ACTIVE".equalsIgnoreCase(account.optString("status", "")) || account.optBoolean("trading_blocked", false)) {
            saveDashboard("hesap bloklu", "Alpaca hesap aktif/trade edilebilir degil; emir gonderilmedi.", recentOrders, baseStats);
            return;
        }
        JSONObject clock = api("GET", "/v2/clock", null, apiKey, secret);
        if (!clock.optBoolean("is_open", false)) {
            saveDashboard("piyasa kapali", "Piyasa kapali; emir gonderilmedi. Hesap bakiyesi guncel.", recentOrders, baseStats);
            return;
        }
        JSONArray openOrders = apiArray("GET", "/v2/orders?status=open&nested=true", apiKey, secret);
        if (hasBlockingOpenOrder(openOrders)) {
            saveDashboard("bekleyen emir", "Acik emir var; tekrar emir gondermemek icin atlandi.", openOrders, withEstimatedNotional(baseStats, 0));
            return;
        }
        Map<String, Double> currentQty = positions(positionsJson);
        double tradingCapital = equity > 0.0 ? equity : CAPITAL;
        double sessionHigh = updateSessionHighEquity(equity);
        boolean blockNewBuys = shouldBlockNewBuys(equity, lastEquity, sessionHigh);
        if (!pureMomentumRebalanceDue(currentQty)) {
            saveDashboard("rebalance bekliyor", "Pure momentum 7 gunluk rebalance penceresini bekliyor; bugun emir gonderilmedi.", recentOrders, withEstimatedNotional(baseStats, 0));
            markEvaluatedToday();
            return;
        }
        PlanResult planResult = buildPlans(currentQty, tradingCapital, apiKey, secret);
        if (alreadyEvaluatedToday()) {
            JSONArray syncedStops = syncProtectiveStops(currentQty, planResult.stopPriceBySymbol, openOrders, apiKey, secret);
            saveDashboard("gunluk tamam", "Bugunun strateji kontrolu yapildi; servis sadece hesap, widget ve stoplari takip ediyor.", syncedStops.length() > 0 ? syncedStops : recentOrders, withEstimatedNotional(baseStats, 0));
            return;
        }
        ArrayList<OrderPlan> plans = planResult.orders;
        if (blockNewBuys) plans = sellOnly(plans);
        plans = safeExecutablePlans(plans, currentQty);
        JSONArray orders = new JSONArray();
        double buyNotional = 0.0;
        for (OrderPlan p : plans) {
            JSONObject row = new JSONObject();
            row.put("symbol", p.symbol); row.put("side", p.side); row.put("qty", String.valueOf(p.qty)); row.put("notional", p.notional); row.put("state", "planned");
            if (p.targetQty > 0.0 && isFractional(p.targetQty)) row.put("target_qty", String.format(Locale.US, "%.6f", p.targetQty));
            if ("buy".equals(p.side) && p.stopPrice > 0.0) row.put("stop_price", String.format(Locale.US, "%.2f", p.stopPrice));
            orders.put(row);
            if ("buy".equals(p.side)) buyNotional += p.notional;
        }
        if (plans.isEmpty()) {
            JSONArray syncedStops = syncProtectiveStops(currentQty, planResult.stopPriceBySymbol, openOrders, apiKey, secret);
            if (syncedStops.length() > 0) {
                saveDashboard("stop senkron", "Mevcut pozisyonlar icin koruyucu stop emirleri guncellendi.", syncedStops, withEstimatedNotional(baseStats, 0));
            } else if (blockNewBuys) {
                saveDashboard("risk limiti", "Gunluk/session kayip limiti nedeniyle yeni alim durduruldu.", recentOrders, withEstimatedNotional(baseStats, 0));
            } else {
                saveDashboard("senkron", "Pozisyonlar strateji hedefleriyle uyumlu.", recentOrders.length() > 0 ? recentOrders : orders, withEstimatedNotional(baseStats, 0));
            }
            markEvaluatedToday();
            markPureMomentumRebalanced();
            return;
        }
        if (buyNotional > buyingPower * BUYING_POWER_LIMIT_MULTIPLIER && !hasSellPlan(plans)) {
            saveDashboard("alim gucu bekliyor", "Alim gucu hedef alislar icin yetmiyor; strateji hedefi bozulmadan sonraki dongude tekrar denenecek.", orders, withEstimatedNotional(baseStats, buyNotional));
            return;
        }
        for (OrderPlan p : plans) validateAsset(p.symbol, p.qty, apiKey, secret);
        cancelProtectiveStopsForPlannedSymbols(openOrders, plans, apiKey, secret);
        JSONArray remainingStops = waitForNoProtectiveStopsForPlannedSymbols(plans, apiKey, secret);
        if (remainingStops.length() > 0) {
            saveDashboard("bekleyen stop", "Koruyucu stop iptali Alpaca tarafinda hala acik gorunuyor; wash trade riskinden dolayi emir gonderilmedi.", remainingStops, withEstimatedNotional(baseStats, buyNotional));
            return;
        }
        plans = sellOrdersFirst(plans);
        JSONArray submitted = new JSONArray();
        boolean submittedSell = false;
        for (OrderPlan p : plans) {
            if ("buy".equals(p.side)) {
                if (submittedSell) {
                    Thread.sleep(2500L);
                }
                double estimatedCost = estimatedOrderCost(p, apiKey, secret);
                JSONObject refreshedAccount = api("GET", "/v2/account", null, apiKey, secret);
                double refreshedBuyingPower = refreshedAccount.optDouble("buying_power", 0.0);
                if (estimatedCost > refreshedBuyingPower * BUYING_POWER_LIMIT_MULTIPLIER) {
                    JSONObject waiting = orderRow(p, "bekliyor: alim gucu");
                    waiting.put("estimated_cost", estimatedCost);
                    waiting.put("buying_power", refreshedBuyingPower);
                    submitted.put(waiting);
                    saveDashboard("alim gucu bekliyor", "Alim gucu hedef alis icin henuz yetmiyor; buy emri atlanmadi, strateji sonraki dongude tekrar deneyecek.", submitted, withEstimatedNotional(baseStats, buyNotional));
                    return;
                }
            } else if (submittedSell) {
                Thread.sleep(2500L);
            }
            JSONObject row = new JSONObject();
            try {
                JSONObject payload = p.toAlpacaOrder();
                JSONObject result = submitOrderWithWashTradeRetry(p.symbol, payload, apiKey, secret);
                row.put("symbol", p.symbol); row.put("side", p.side); row.put("qty", String.valueOf(p.qty)); row.put("notional", p.notional); row.put("state", result.optString("status", "submitted"));
                if (p.targetQty > 0.0 && isFractional(p.targetQty)) row.put("target_qty", String.format(Locale.US, "%.6f", p.targetQty));
                if ("buy".equals(p.side) && p.stopPrice > 0.0) row.put("stop_price", String.format(Locale.US, "%.2f", p.stopPrice));
                submitted.put(row);
                if ("sell".equals(p.side)) submittedSell = true;
            } catch (Exception e) {
                if ("buy".equals(p.side) && isInsufficientBuyingPowerReject(e)) {
                    JSONObject waiting = orderRow(p, "bekliyor: alim gucu");
                    waiting.put("error", e.getMessage());
                    submitted.put(waiting);
                    saveDashboard("alim gucu bekliyor", "Alpaca alim gucu yetersiz dedi; buy emri atlanmadi, strateji sonraki dongude ayni hedefi tekrar deneyecek.", submitted, withEstimatedNotional(baseStats, buyNotional));
                    return;
                }
                row.put("symbol", p.symbol); row.put("side", p.side); row.put("qty", String.valueOf(p.qty)); row.put("notional", p.notional); row.put("state", "hata: " + e.getMessage());
                submitted.put(row);
                saveDashboard("emir reddi", "Alpaca emri reddetti; sonraki dongude tekrar hesap/pozisyon kontrolu yapilacak. " + e.getMessage(), submitted, withEstimatedNotional(baseStats, buyNotional));
                return;
            }
        }
        Thread.sleep(2000L);
        JSONArray refreshedOpenOrders = apiArray("GET", "/v2/orders?status=open&nested=true", apiKey, secret);
        JSONArray refreshedPositions = apiArray("GET", "/v2/positions", apiKey, secret);
        JSONArray syncedStops = syncProtectiveStops(positions(refreshedPositions), planResult.stopPriceBySymbol, refreshedOpenOrders, apiKey, secret);
        for (int i = 0; i < syncedStops.length(); i++) submitted.put(syncedStops.get(i));
        saveDashboard("gonderildi", "Emirler gonderildi; koruyucu stoplar tum pozisyona gore senkronlandi.", submitted, withEstimatedNotional(baseStats, buyNotional));
        markEvaluatedToday();
        markPureMomentumRebalanced();
    }

    private boolean hasSellPlan(ArrayList<OrderPlan> plans) {
        for (OrderPlan p : plans) {
            if ("sell".equals(p.side)) return true;
        }
        return false;
    }

    private JSONObject orderRow(OrderPlan p, String state) throws Exception {
        JSONObject row = new JSONObject();
        row.put("symbol", p.symbol);
        row.put("side", p.side);
        row.put("qty", String.valueOf(p.qty));
        row.put("notional", p.notional);
        row.put("state", state);
        if (p.targetQty > 0.0 && isFractional(p.targetQty)) row.put("target_qty", String.format(Locale.US, "%.6f", p.targetQty));
        if ("buy".equals(p.side) && p.stopPrice > 0.0) row.put("stop_price", String.format(Locale.US, "%.2f", p.stopPrice));
        return row;
    }

    private double estimatedOrderCost(OrderPlan p, String apiKey, String secret) {
        if (!"buy".equals(p.side)) return p.notional;
        double fallback = p.qty > 0 ? p.notional / p.qty : 0.0;
        double livePrice = fetchLivePrice(p.symbol, fallback, apiKey, secret);
        return p.qty * (livePrice > 0.0 ? livePrice : fallback);
    }

    private PlanResult buildPlans(Map<String, Double> currentQty, double capital, String apiKey, String secret) throws Exception {
        Map<String, Series> data = new HashMap<>();
        for (String symbol : PURE_MOMENTUM_UNIVERSE) data.put(symbol, fetchYahoo(symbol));
        data.put(REGIME_SYMBOL, fetchYahoo(REGIME_SYMBOL));
        StrategySelection selection = selectStrategySymbols(data);
        ArrayList<String> selected = selection.symbols;

        Map<String, Double> exposure = new HashMap<>();
        Map<String, Double> lastClose = new HashMap<>();
        Map<String, Double> livePrice = new HashMap<>();
        Map<String, Double> stopPrice = new HashMap<>();
        int count = selected.size();
        double eachExposure = count > 0 ? selection.targetGross / count : 0.0;
        for (String symbol : selected) exposure.put(symbol, eachExposure);
        for (String symbol : managedSymbols(currentQty)) {
            Series s = data.containsKey(symbol) ? data.get(symbol) : fetchYahoo(symbol);
            lastClose.put(symbol, s.close[s.close.length - 1]);
            livePrice.put(symbol, fetchLivePrice(symbol, s.close[s.close.length - 1], apiKey, secret));
            if (!exposure.containsKey(symbol)) exposure.put(symbol, 0.0);
        }
        double gross = 0.0;
        for (double targetExposure : exposure.values()) gross += Math.abs(targetExposure);
        double scale = gross > 0.0 ? Math.min(1.0, GROSS_CAP / gross) : 1.0;
        ArrayList<OrderPlan> plans = new ArrayList<>();
        for (String symbol : exposure.keySet()) {
            double px = livePrice.containsKey(symbol) && livePrice.get(symbol) > 0.0 ? livePrice.get(symbol) : lastClose.get(symbol);
            double targetQty = capital * exposure.get(symbol) * scale / px;
            double delta = targetQty - (currentQty.containsKey(symbol) ? currentQty.get(symbol) : 0.0);
            if (Math.abs(delta) < 0.0001) continue;
            boolean sell = delta < 0.0;
            int qty = wholeShareOrderQuantity(Math.abs(delta));
            if (sell) {
                int heldQty = wholeShareOrderQuantity(Math.abs(currentQty.containsKey(symbol) ? currentQty.get(symbol) : 0.0));
                qty = Math.min(qty, heldQty);
            }
            if (qty < 1) continue;
            double notional = qty * px;
            if (notional < MIN_DELTA_NOTIONAL) continue;
            plans.add(new OrderPlan(symbol, sell ? "sell" : "buy", qty, notional, stopPrice.containsKey(symbol) ? stopPrice.get(symbol) : 0.0, targetQty));
        }
        return new PlanResult(plans, stopPrice);
    }

    private StrategySelection selectStrategySymbols(Map<String, Series> data) {
        double breadth20 = breadth20(data);
        double spy20 = momentum(data.get(REGIME_SYMBOL).close, 20);
        double spyDd63 = drawdownFromHigh(data.get(REGIME_SYMBOL).close, 63);
        if (spy20 < -0.05) return new StrategySelection(rankSymbols(data, 5, 7, 0.0, null), PURE_MOMENTUM_TARGET_GROSS, "loss_spy20_crash_lb5_top7");
        if (spyDd63 >= -0.05 && spyDd63 <= -0.02) return new StrategySelection(rankSymbols(data, 63, 5, 0.0, null), PURE_MOMENTUM_TARGET_GROSS, "loss_spy_dd_2_5_lb63_top5");
        if (breadth20 < WEAK_BREADTH_THRESHOLD) return new StrategySelection(rankSymbols(data, 63, 5, 0.0, null), PURE_MOMENTUM_TARGET_GROSS, "loss_weak_breadth_lb63_top5");
        if (spy20 >= -0.02 && spy20 < 0.0) return new StrategySelection(rankSymbols(data, 63, 5, 0.0, null), PURE_MOMENTUM_TARGET_GROSS, "loss_spy20_mild_neg_lb63_top5");
        if (breadth20 >= MID_BREADTH_CASH_LOW && breadth20 < MID_BREADTH_CASH_HIGH && spy20 >= MID_BREADTH_SLEEVE_SPY20_MIN) {
            return new StrategySelection(rankSymbols(data, 63, MID_BREADTH_SLEEVE_TOP_N, 0.0, null), MID_BREADTH_SLEEVE_TARGET_GROSS, "normal_mid_breadth_spy20_top3_gross08");
        }
        if (breadth20 >= MID_BREADTH_CASH_LOW && breadth20 < MID_BREADTH_CASH_HIGH) return new StrategySelection(new ArrayList<String>(), 0.0, "normal_mid_breadth_cash");
        return new StrategySelection(rankSymbols(data, PURE_MOMENTUM_LOOKBACK_DAYS, PURE_MOMENTUM_TOP_N, 0.0, NORMAL_MIN_5D_MOMENTUM), PURE_MOMENTUM_TARGET_GROSS, "normal_lb63_top7_mom5_pos");
    }

    private ArrayList<String> rankSymbols(Map<String, Series> data, int lookbackDays, int topN, Double minMomentum, Double minShortMomentum) {
        ArrayList<MomentumRank> ranks = new ArrayList<>();
        for (String symbol : PURE_MOMENTUM_UNIVERSE) {
            Series series = data.get(symbol);
            if (series == null) continue;
            double value = momentum(series.close, lookbackDays);
            if (Double.isNaN(value)) continue;
            if (minMomentum != null && value < minMomentum) continue;
            if (minShortMomentum != null) {
                double shortMomentum = momentum(series.close, 5);
                if (Double.isNaN(shortMomentum) || shortMomentum < minShortMomentum) continue;
            }
            ranks.add(new MomentumRank(symbol, value));
        }
        ranks.sort((a, b) -> Double.compare(b.momentum, a.momentum));
        ArrayList<String> selected = new ArrayList<>();
        int count = Math.min(topN, ranks.size());
        for (int i = 0; i < count; i++) selected.add(ranks.get(i).symbol);
        return selected;
    }

    private double breadth20(Map<String, Series> data) {
        int valid = 0;
        int positive = 0;
        for (String symbol : PURE_MOMENTUM_UNIVERSE) {
            Series series = data.get(symbol);
            if (series == null) continue;
            double value = momentum(series.close, 20);
            if (Double.isNaN(value)) continue;
            valid++;
            if (value > 0.0) positive++;
        }
        return valid > 0 ? ((double) positive) / valid : 0.0;
    }

    private ArrayList<OrderPlan> safeExecutablePlans(ArrayList<OrderPlan> plans, Map<String, Double> currentQty) {
        ArrayList<OrderPlan> out = new ArrayList<>();
        for (OrderPlan plan : plans) {
            if (plan.qty < 1 || plan.notional < MIN_DELTA_NOTIONAL) continue;
            if ("sell".equals(plan.side)) {
                int heldQty = wholeShareOrderQuantity(Math.abs(currentQty.containsKey(plan.symbol) ? currentQty.get(plan.symbol) : 0.0));
                if (heldQty < 1) continue;
                if (plan.qty > heldQty) plan.qty = heldQty;
                plan.notional = Math.max(MIN_DELTA_NOTIONAL, plan.notional);
            }
            out.add(plan);
        }
        return out;
    }

    private ArrayList<OrderPlan> sellOrdersFirst(ArrayList<OrderPlan> plans) {
        ArrayList<OrderPlan> ordered = new ArrayList<>();
        for (OrderPlan plan : plans) if ("sell".equals(plan.side)) ordered.add(plan);
        for (OrderPlan plan : plans) if (!"sell".equals(plan.side)) ordered.add(plan);
        return ordered;
    }

    private ArrayList<String> managedSymbols(Map<String, Double> currentQty) {
        ArrayList<String> out = new ArrayList<>();
        for (String symbol : PURE_MOMENTUM_UNIVERSE) if (!out.contains(symbol)) out.add(symbol);
        for (String symbol : LEGACY_MANAGED_SYMBOLS) if (!out.contains(symbol)) out.add(symbol);
        for (String symbol : currentQty.keySet()) if (contains(PURE_MOMENTUM_UNIVERSE, symbol) || contains(LEGACY_MANAGED_SYMBOLS, symbol)) {
            if (!out.contains(symbol)) out.add(symbol);
        }
        return out;
    }

    private boolean contains(String[] symbols, String symbol) {
        for (String item : symbols) if (item.equalsIgnoreCase(symbol)) return true;
        return false;
    }

    private int wholeShareOrderQuantity(double quantity) {
        return (int) Math.floor(quantity);
    }

    private double updateSessionHighEquity(double equity) {
        double previous = Double.longBitsToDouble(prefs.getLong("session_high_equity_bits", Double.doubleToLongBits(0.0)));
        double high = Math.max(previous, equity);
        prefs.edit().putLong("session_high_equity_bits", Double.doubleToLongBits(high)).apply();
        return high;
    }

    private boolean shouldBlockNewBuys(double equity, double lastEquity, double sessionHigh) {
        boolean dailyLoss = lastEquity > 0.0 && equity < lastEquity * (1.0 - DAILY_BUY_BLOCK_LOSS_PCT);
        boolean sessionDrawdown = sessionHigh > 0.0 && equity < sessionHigh * (1.0 - SESSION_BUY_BLOCK_DRAWDOWN_PCT);
        return dailyLoss || sessionDrawdown;
    }

    private ArrayList<OrderPlan> sellOnly(ArrayList<OrderPlan> plans) {
        ArrayList<OrderPlan> filtered = new ArrayList<>();
        for (OrderPlan plan : plans) if ("sell".equals(plan.side)) filtered.add(plan);
        return filtered;
    }

    private boolean isFractional(double quantity) {
        return Math.abs(quantity - Math.rint(quantity)) > 0.000001;
    }

    private double capStopBelowBasePrice(double stopPrice, double basePrice) {
        if (basePrice <= 0.02) return Math.max(0.01, stopPrice);
        double bufferedCap = Math.min(basePrice - 0.05, basePrice * (1.0 - MIN_PROTECTIVE_STOP_DISTANCE_PCT));
        return Math.max(0.01, Math.min(stopPrice, bufferedCap));
    }

    private double momentum(double[] close, int lookbackDays) {
        int i = close.length - 1;
        if (i < lookbackDays || close[i - lookbackDays] == 0.0) return Double.NaN;
        return close[i] / close[i - lookbackDays] - 1.0;
    }

    private double drawdownFromHigh(double[] close, int lookbackDays) {
        int last = close.length - 1;
        if (last < lookbackDays) return Double.NaN;
        double high = 0.0;
        for (int i = last - lookbackDays + 1; i <= last; i++) high = Math.max(high, close[i]);
        return high > 0.0 ? close[last] / high - 1.0 : Double.NaN;
    }

    private Series fetchYahoo(String symbol) throws Exception {
        String url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?range=2y&interval=1d&events=history";
        JSONObject json = httpJson(url);
        JSONObject result = json.getJSONObject("chart").getJSONArray("result").getJSONObject(0);
        JSONArray closeJson = result.getJSONObject("indicators").getJSONArray("quote").getJSONObject(0).getJSONArray("close");
        JSONArray timestampJson = result.optJSONArray("timestamp");
        ArrayList<Double> vals = new ArrayList<>();
        for (int i = 0; i < closeJson.length(); i++) {
            if (closeJson.isNull(i)) continue;
            if (timestampJson != null && i == closeJson.length() - 1 && isNewYorkToday(timestampJson.optLong(i, 0L))) continue;
            vals.add(closeJson.getDouble(i));
        }
        double[] close = new double[vals.size()];
        for (int i = 0; i < vals.size(); i++) close[i] = vals.get(i);
        return new Series(close);
    }

    private double fetchLivePrice(String symbol, double fallback, String key, String secret) {
        double alpacaPrice = fetchAlpacaLatestPrice(symbol, key, secret);
        if (alpacaPrice > 0.0) return alpacaPrice;
        try {
            String url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?range=1d&interval=1m";
            JSONObject json = httpJson(url);
            JSONObject result = json.getJSONObject("chart").getJSONArray("result").getJSONObject(0);
            JSONArray closeJson = result.getJSONObject("indicators").getJSONArray("quote").getJSONObject(0).getJSONArray("close");
            for (int i = closeJson.length() - 1; i >= 0; i--) {
                if (!closeJson.isNull(i)) return closeJson.getDouble(i);
            }
        } catch (Exception ignored) {
        }
        return fallback * 0.99;
    }

    private double fetchAlpacaLatestPrice(String symbol, String key, String secret) {
        try {
            JSONObject json = dataApi("GET", "/v2/stocks/" + symbol + "/trades/latest?feed=iex", key, secret);
            JSONObject trade = json.optJSONObject("trade");
            if (trade != null) return trade.optDouble("p", 0.0);
        } catch (Exception ignored) {
        }
        return 0.0;
    }

    private boolean isNewYorkToday(long epochSeconds) {
        if (epochSeconds <= 0L) return false;
        TimeZone ny = TimeZone.getTimeZone("America/New_York");
        Calendar bar = Calendar.getInstance(ny);
        bar.setTimeInMillis(epochSeconds * 1000L);
        Calendar now = Calendar.getInstance(ny);
        return bar.get(Calendar.YEAR) == now.get(Calendar.YEAR)
            && bar.get(Calendar.DAY_OF_YEAR) == now.get(Calendar.DAY_OF_YEAR);
    }

    private JSONObject httpJson(String urlText) throws Exception {
        throttle();
        HttpURLConnection c = (HttpURLConnection) new URL(urlText).openConnection();
        c.setConnectTimeout(30000); c.setReadTimeout(30000); c.setRequestProperty("User-Agent", "QuantPaperBot/1.0");
        return new JSONObject(read(c));
    }

    private JSONObject api(String method, String path, JSONObject body, String key, String secret) throws Exception {
        for (int attempt = 0; attempt < 4; attempt++) {
            throttle();
            HttpURLConnection c = (HttpURLConnection) new URL(BASE_URL + path).openConnection();
            c.setRequestMethod(method); c.setConnectTimeout(30000); c.setReadTimeout(30000);
            c.setRequestProperty("APCA-API-KEY-ID", key); c.setRequestProperty("APCA-API-SECRET-KEY", secret); c.setRequestProperty("Content-Type", "application/json");
            if (body != null) { c.setDoOutput(true); OutputStream os = c.getOutputStream(); os.write(body.toString().getBytes(StandardCharsets.UTF_8)); os.close(); }
            int code = c.getResponseCode();
            if (code != 429 && code < 500) {
                String text = read(c);
                return text.isEmpty() ? new JSONObject() : new JSONObject(text);
            }
            Thread.sleep(Math.min(8000, 1000L * (1L << attempt)));
        }
        throw new RuntimeException("Alpaca API retry limit reached: " + path);
    }

    private JSONObject dataApi(String method, String path, String key, String secret) throws Exception {
        for (int attempt = 0; attempt < 4; attempt++) {
            throttle();
            HttpURLConnection c = (HttpURLConnection) new URL(DATA_URL + path).openConnection();
            c.setRequestMethod(method); c.setConnectTimeout(30000); c.setReadTimeout(30000);
            c.setRequestProperty("APCA-API-KEY-ID", key); c.setRequestProperty("APCA-API-SECRET-KEY", secret); c.setRequestProperty("Content-Type", "application/json");
            int code = c.getResponseCode();
            if (code != 429 && code < 500) {
                String text = read(c);
                return text.isEmpty() ? new JSONObject() : new JSONObject(text);
            }
            Thread.sleep(Math.min(8000, 1000L * (1L << attempt)));
        }
        throw new RuntimeException("Alpaca data retry limit reached: " + path);
    }

    private JSONArray apiArray(String method, String path, String key, String secret) throws Exception {
        for (int attempt = 0; attempt < 4; attempt++) {
            throttle();
            HttpURLConnection c = (HttpURLConnection) new URL(BASE_URL + path).openConnection();
            c.setRequestMethod(method); c.setConnectTimeout(30000); c.setReadTimeout(30000);
            c.setRequestProperty("APCA-API-KEY-ID", key); c.setRequestProperty("APCA-API-SECRET-KEY", secret); c.setRequestProperty("Content-Type", "application/json");
            int code = c.getResponseCode();
            if (code != 429 && code < 500) return new JSONArray(read(c));
            Thread.sleep(Math.min(8000, 1000L * (1L << attempt)));
        }
        throw new RuntimeException("Alpaca API retry limit reached: " + path);
    }

    private String read(HttpURLConnection c) throws Exception {
        BufferedReader br = new BufferedReader(new InputStreamReader(c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream(), StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder(); String line;
        while ((line = br.readLine()) != null) sb.append(line);
        br.close();
        if (c.getResponseCode() >= 400) throw new RuntimeException("HTTP " + c.getResponseCode() + ": " + sb);
        return sb.toString();
    }

    private void throttle() throws InterruptedException {
        long now = System.currentTimeMillis();
        long wait = 350L - (now - lastRequestAt);
        if (wait > 0) Thread.sleep(wait);
        lastRequestAt = System.currentTimeMillis();
    }

    private Map<String, Double> positions(JSONArray arr) {
        Map<String, Double> out = new HashMap<>();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject p = arr.optJSONObject(i); if (p != null) out.put(p.optString("symbol"), p.optDouble("qty", 0.0));
        }
        return out;
    }

    private boolean hasBlockingOpenOrder(JSONArray openOrders) {
        for (int i = 0; i < openOrders.length(); i++) {
            JSONObject order = openOrders.optJSONObject(i);
            if (order == null) continue;
            for (JSONObject item : orderAndLegs(order)) {
                if (!isProtectiveStop(item)) return true;
            }
        }
        return false;
    }

    private boolean isProtectiveStop(JSONObject order) {
        return "sell".equalsIgnoreCase(order.optString("side")) && "stop".equalsIgnoreCase(order.optString("type"));
    }

    private void cancelProtectiveStopsForPlannedSymbols(JSONArray openOrders, ArrayList<OrderPlan> plans, String key, String secret) throws Exception {
        Set<String> cancelled = new HashSet<>();
        for (OrderPlan plan : plans) {
            for (int i = 0; i < openOrders.length(); i++) {
                JSONObject order = openOrders.optJSONObject(i);
                if (order == null) continue;
                for (JSONObject item : orderAndLegs(order)) {
                    if (!isProtectiveStop(item)) continue;
                    if (!plan.symbol.equalsIgnoreCase(item.optString("symbol"))) continue;
                    String id = item.optString("id");
                    if (id.isEmpty() || cancelled.contains(id)) continue;
                    cancelOrder(id, key, secret);
                    cancelled.add(id);
                }
            }
        }
    }

    private JSONArray waitForNoProtectiveStopsForPlannedSymbols(ArrayList<OrderPlan> plans, String key, String secret) throws Exception {
        long end = System.currentTimeMillis() + CANCEL_WAIT_MS;
        JSONArray remaining = new JSONArray();
        while (true) {
            JSONArray openOrders = apiArray("GET", "/v2/orders?status=open&nested=true", key, secret);
            remaining = protectiveStopsForPlannedSymbols(openOrders, plans);
            if (remaining.length() == 0 || System.currentTimeMillis() >= end) return remaining;
            Thread.sleep(CANCEL_POLL_MS);
        }
    }

    private JSONArray protectiveStopsForPlannedSymbols(JSONArray openOrders, ArrayList<OrderPlan> plans) {
        JSONArray out = new JSONArray();
        Set<String> seen = new HashSet<>();
        for (OrderPlan plan : plans) {
            for (int i = 0; i < openOrders.length(); i++) {
                JSONObject order = openOrders.optJSONObject(i);
                if (order == null) continue;
                for (JSONObject item : orderAndLegs(order)) {
                    String id = item.optString("id");
                    if (!isProtectiveStop(item) || !plan.symbol.equalsIgnoreCase(item.optString("symbol")) || seen.contains(id)) continue;
                    out.put(item);
                    seen.add(id);
                }
            }
        }
        return out;
    }

    private JSONArray syncProtectiveStops(Map<String, Double> currentQty, Map<String, Double> stopPriceBySymbol, JSONArray openOrders, String key, String secret) throws Exception {
        JSONArray submitted = new JSONArray();
        for (String symbol : stopPriceBySymbol.keySet()) {
            int qty = wholeShareOrderQuantity(Math.abs(currentQty.containsKey(symbol) ? currentQty.get(symbol) : 0.0));
            double stopPrice = capStopBelowBasePrice(stopPriceBySymbol.get(symbol), fetchLivePrice(symbol, stopPriceBySymbol.get(symbol), key, secret));
            if (qty < 1 || stopPrice <= 0.0) continue;
            JSONObject existing = protectiveStopForSymbol(openOrders, symbol);
            if (existing != null && Math.abs(existing.optDouble("stop_price", 0.0) - stopPrice) < 0.01 && Math.abs(existing.optDouble("qty", qty) - qty) < 0.000001) continue;
            if (existing != null) {
                cancelOrder(existing.optString("id"), key, secret);
                JSONArray remaining = waitForNoProtectiveStopsForSymbols(new String[] { symbol }, key, secret);
                if (remaining.length() > 0) continue;
            }
            JSONObject payload = protectiveStopOrder(symbol, qty, stopPrice);
            JSONObject result = submitOrderWithWashTradeRetry(symbol, payload, key, secret);
            JSONObject row = new JSONObject();
            row.put("symbol", symbol); row.put("side", "sell"); row.put("qty", String.valueOf(qty)); row.put("notional", qty * stopPrice); row.put("state", result.optString("status", "submitted"));
            row.put("stop_price", String.format(Locale.US, "%.2f", stopPrice));
            submitted.put(row);
        }
        return submitted;
    }

    private JSONObject protectiveStopForSymbol(JSONArray openOrders, String symbol) {
        for (int i = 0; i < openOrders.length(); i++) {
            JSONObject order = openOrders.optJSONObject(i);
            if (order == null) continue;
            for (JSONObject item : orderAndLegs(order)) {
                if (isProtectiveStop(item) && symbol.equalsIgnoreCase(item.optString("symbol"))) return item;
            }
        }
        return null;
    }

    private ArrayList<JSONObject> orderAndLegs(JSONObject order) {
        ArrayList<JSONObject> out = new ArrayList<>();
        collectOrderAndLegs(order, out);
        return out;
    }

    private void collectOrderAndLegs(JSONObject order, ArrayList<JSONObject> out) {
        out.add(order);
        JSONArray legs = order.optJSONArray("legs");
        if (legs == null) return;
        for (int i = 0; i < legs.length(); i++) {
            JSONObject leg = legs.optJSONObject(i);
            if (leg != null) collectOrderAndLegs(leg, out);
        }
    }

    private JSONArray waitForNoProtectiveStopsForSymbols(String[] symbols, String key, String secret) throws Exception {
        long end = System.currentTimeMillis() + CANCEL_WAIT_MS;
        JSONArray remaining = new JSONArray();
        while (true) {
            JSONArray openOrders = apiArray("GET", "/v2/orders?status=open&nested=true", key, secret);
            remaining = protectiveStopsForSymbols(openOrders, symbols);
            if (remaining.length() == 0 || System.currentTimeMillis() >= end) return remaining;
            Thread.sleep(CANCEL_POLL_MS);
        }
    }

    private JSONArray protectiveStopsForSymbols(JSONArray openOrders, String[] symbols) {
        JSONArray out = new JSONArray();
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < openOrders.length(); i++) {
            JSONObject order = openOrders.optJSONObject(i);
            if (order == null) continue;
            for (JSONObject item : orderAndLegs(order)) {
                String id = item.optString("id");
                if (!isProtectiveStop(item) || seen.contains(id)) continue;
                for (String symbol : symbols) {
                    if (!symbol.equalsIgnoreCase(item.optString("symbol"))) continue;
                    out.put(item);
                    seen.add(id);
                    break;
                }
            }
        }
        return out;
    }

    private JSONObject submitOrderWithWashTradeRetry(String symbol, JSONObject payload, String key, String secret) throws Exception {
        try {
            return api("POST", "/v2/orders", payload, key, secret);
        } catch (Exception e) {
            if (!isWashTradeReject(e)) throw e;
            JSONArray openOrders = apiArray("GET", "/v2/orders?status=open&nested=true", key, secret);
            ArrayList<OrderPlan> plans = new ArrayList<>();
            plans.add(new OrderPlan(symbol, "sell", 1, MIN_DELTA_NOTIONAL, 0.0, 0.0));
            cancelProtectiveStopsForPlannedSymbols(openOrders, plans, key, secret);
            JSONArray remaining = waitForNoProtectiveStopsForSymbols(new String[] { symbol }, key, secret);
            if (remaining.length() > 0) throw new RuntimeException(symbol + ": protective stop hala acik; wash trade retry atlandi. " + remaining);
            return api("POST", "/v2/orders", payload, key, secret);
        }
    }

    private boolean isWashTradeReject(Exception e) {
        String message = e.getMessage() == null ? "" : e.getMessage().toLowerCase(Locale.US);
        return message.contains("potential wash trade") || message.contains("opposite side market/stop order exists");
    }

    private boolean isInsufficientBuyingPowerReject(Exception e) {
        String message = e.getMessage() == null ? "" : e.getMessage().toLowerCase(Locale.US);
        return message.contains("insufficient buying power") || message.contains("\"code\":40310000");
    }

    private JSONObject protectiveStopOrder(String symbol, int qty, double stopPrice) throws Exception {
        JSONObject o = new JSONObject();
        o.put("symbol", symbol);
        o.put("qty", String.valueOf(qty));
        o.put("side", "sell");
        o.put("type", "stop");
        o.put("time_in_force", "gtc");
        o.put("stop_price", String.format(Locale.US, "%.2f", stopPrice));
        o.put("client_order_id", "android-stop-" + symbol + "-" + System.currentTimeMillis());
        return o;
    }

    private void cancelOrder(String orderId, String key, String secret) throws Exception {
        if (orderId == null || orderId.isEmpty()) return;
        api("DELETE", "/v2/orders/" + orderId, null, key, secret);
    }

    private JSONArray recentOrders(String key, String secret) {
        try {
            return apiArray("GET", "/v2/orders?status=all&limit=50&direction=desc&nested=true", key, secret);
        } catch (Exception ignored) {
            return new JSONArray();
        }
    }

    private JSONObject portfolioHistory(String key, String secret) {
        try {
            return api("GET", "/v2/account/portfolio/history?period=1M&timeframe=1D", null, key, secret);
        } catch (Exception ignored) {
            return new JSONObject();
        }
    }

    private void validateAsset(String symbol, double qty, String key, String secret) throws Exception {
        JSONObject asset = api("GET", "/v2/assets/" + symbol, null, key, secret);
        if (!asset.optBoolean("tradable", false)) throw new RuntimeException(symbol + " is not tradable.");
        if (Math.abs(qty - Math.rint(qty)) > 0.000001 && !asset.optBoolean("fractionable", false)) throw new RuntimeException(symbol + " is not fractionable.");
    }

    private JSONObject stats(double notional, double buyingPower, String loop) throws Exception {
        return stats(notional, buyingPower, 0.0, loop);
    }

    private JSONObject stats(double notional, double buyingPower, double cash, String loop) throws Exception {
        return stats(notional, buyingPower, cash, 0.0, 0.0, loop, new JSONArray(), new JSONArray(), new JSONObject());
    }

    private JSONObject stats(double notional, double buyingPower, double cash, double equity, double lastEquity, String loop, JSONArray recentOrders, JSONArray positions, JSONObject portfolioHistory) throws Exception {
        JSONObject s = new JSONObject();
        s.put("capital", CAPITAL);
        s.put("estimated_buy_notional", notional);
        s.put("buying_power", buyingPower);
        s.put("cash", cash);
        s.put("equity", equity);
        s.put("last_equity", lastEquity);
        s.put("daily_pl", equity - lastEquity);
        s.put("weekly_pl", periodProfit(portfolioHistory, 5));
        s.put("monthly_pl", periodProfit(portfolioHistory, 21));
        s.put("loop", loop);
        s.put("recent_orders", recentOrders);
        s.put("positions", positions);
        s.put("portfolio_history", portfolioHistory);
        return s;
    }

    private JSONObject withEstimatedNotional(JSONObject stats, double notional) throws Exception {
        stats.put("estimated_buy_notional", notional);
        return stats;
    }

    private double periodProfit(JSONObject history, int daysBack) {
        try {
            JSONArray equity = history.optJSONArray("equity");
            if (equity == null || equity.length() < 2) return 0.0;
            ArrayList<Double> valid = new ArrayList<>();
            for (int i = 0; i < equity.length(); i++) {
                double value = Double.parseDouble(equity.optString(i, "0"));
                if (value > 1.0) valid.add(value);
            }
            if (valid.size() < 2) return 0.0;
            int last = valid.size() - 1;
            int start = Math.max(0, last - daysBack);
            return valid.get(last) - valid.get(start);
        } catch (Exception ignored) {
            return 0.0;
        }
    }

    private boolean alreadyEvaluatedToday() {
        return newYorkDateKey().equals(prefs.getString("last_strategy_date", ""));
    }

    private void markEvaluatedToday() {
        prefs.edit().putString("last_strategy_date", newYorkDateKey()).apply();
    }

    private boolean pureMomentumRebalanceDue(Map<String, Double> currentQty) {
        if (!hasManagedPosition(currentQty)) return true;
        String last = prefs.getString("last_puremom_rebalance_date", "");
        if (last == null || last.isEmpty()) return true;
        return tradingDaysSince(last, newYorkDateKey()) >= PURE_MOMENTUM_REBALANCE_DAYS;
    }

    private boolean hasManagedPosition(Map<String, Double> currentQty) {
        for (String symbol : currentQty.keySet()) {
            double qty = Math.abs(currentQty.containsKey(symbol) ? currentQty.get(symbol) : 0.0);
            if (qty > 0.0001 && (contains(PURE_MOMENTUM_UNIVERSE, symbol) || contains(LEGACY_MANAGED_SYMBOLS, symbol))) return true;
        }
        return false;
    }

    private int tradingDaysSince(String start, String end) {
        try {
            String[] a = start.split("-");
            String[] b = end.split("-");
            Calendar s = Calendar.getInstance(TimeZone.getTimeZone("America/New_York"));
            Calendar e = Calendar.getInstance(TimeZone.getTimeZone("America/New_York"));
            s.set(Integer.parseInt(a[0]), Integer.parseInt(a[1]) - 1, Integer.parseInt(a[2]), 0, 0, 0);
            e.set(Integer.parseInt(b[0]), Integer.parseInt(b[1]) - 1, Integer.parseInt(b[2]), 0, 0, 0);
            int days = 0;
            while (s.before(e)) {
                s.add(Calendar.DAY_OF_MONTH, 1);
                int dow = s.get(Calendar.DAY_OF_WEEK);
                if (dow != Calendar.SATURDAY && dow != Calendar.SUNDAY) days++;
            }
            return days;
        } catch (Exception ignored) {
            return PURE_MOMENTUM_REBALANCE_DAYS;
        }
    }

    private void markPureMomentumRebalanced() {
        prefs.edit().putString("last_puremom_rebalance_date", newYorkDateKey()).apply();
    }

    private String newYorkDateKey() {
        Calendar now = Calendar.getInstance(TimeZone.getTimeZone("America/New_York"));
        return String.format(Locale.US, "%04d-%02d-%02d", now.get(Calendar.YEAR), now.get(Calendar.MONTH) + 1, now.get(Calendar.DAY_OF_MONTH));
    }

    private void saveDashboard(String status, String message, JSONArray orders, JSONObject stats) {
        try { stats.put("running", prefs.getBoolean("running", true)); } catch (Exception ignored) {}
        prefs.edit()
            .putString("dashboard_html", DashboardRenderer.render(status, message == null ? "" : message, orders.toString(), stats.toString()))
            .putString("widget_status", status == null ? "" : status)
            .putString("widget_message", message == null ? "" : message)
            .putString("widget_orders", orders.toString())
            .putString("widget_stats", stats.toString())
            .putLong("widget_updated_at", System.currentTimeMillis())
            .apply();
        QuantBotWidgetProvider.updateAll(this);
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.notify(1, notification(status + ": " + (message == null ? "" : message)));
    }

    private void sleepInterruptibly(long ms) {
        long end = System.currentTimeMillis() + ms;
        while (!stopRequested && prefs.getBoolean("running", true) && System.currentTimeMillis() < end) {
            try { Thread.sleep(Math.min(2000, end - System.currentTimeMillis())); } catch (InterruptedException ignored) { return; }
        }
    }

    private Notification notification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL_ID) : new Notification.Builder(this);
        b.setContentTitle("Pure Momentum 63")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_pure_momentum)
            .setOngoing(true)
            .setAutoCancel(false)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setPriority(Notification.PRIORITY_HIGH)
            .setContentIntent(pi)
            .setShowWhen(true)
            .setOnlyAlertOnce(true);
        if (Build.VERSION.SDK_INT >= 31) b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        return b.build();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "Pure Momentum 63 Aktif", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("Bot calisirken kapanmayan kalici bildirim gosterir.");
            ch.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(ch);
        }
    }

    static class Series { double[] close; Series(double[] close) { this.close = close; } }
    static class MomentumRank {
        String symbol; double momentum;
        MomentumRank(String symbol, double momentum) { this.symbol = symbol; this.momentum = momentum; }
    }
    static class StrategySelection {
        ArrayList<String> symbols;
        double targetGross;
        String mode;
        StrategySelection(ArrayList<String> symbols, double targetGross, String mode) {
            this.symbols = symbols;
            this.targetGross = targetGross;
            this.mode = mode;
        }
    }
    static class PlanResult {
        ArrayList<OrderPlan> orders;
        Map<String, Double> stopPriceBySymbol;
        PlanResult(ArrayList<OrderPlan> orders, Map<String, Double> stopPriceBySymbol) {
            this.orders = orders;
            this.stopPriceBySymbol = stopPriceBySymbol;
        }
    }
    static class OrderPlan {
        String symbol, side; int qty; double notional, stopPrice, targetQty;
        OrderPlan(String symbol, String side, int qty, double notional, double stopPrice, double targetQty) { this.symbol = symbol; this.side = side; this.qty = qty; this.notional = notional; this.stopPrice = stopPrice; this.targetQty = targetQty; }
        JSONObject toAlpacaOrder() throws Exception {
            JSONObject o = new JSONObject(); o.put("symbol", symbol); o.put("qty", String.valueOf(qty)); o.put("side", side); o.put("type", "market"); o.put("time_in_force", "day");
            o.put("client_order_id", "android-puremom-" + symbol + "-" + System.currentTimeMillis());
            return o;
        }
    }
}
