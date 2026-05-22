package com.quant.paperbot;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.Locale;

public class QuantBotWidgetProvider extends AppWidgetProvider {
    @Override public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        updateWidgets(context, manager, appWidgetIds);
    }

    public static void updateAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        ComponentName widget = new ComponentName(context, QuantBotWidgetProvider.class);
        updateWidgets(context, manager, manager.getAppWidgetIds(widget));
    }

    private static void updateWidgets(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        if (appWidgetIds == null || appWidgetIds.length == 0) return;
        SharedPreferences prefs = context.getSharedPreferences("quant_bot", Context.MODE_PRIVATE);
        boolean running = prefs.getBoolean("running", false);
        String status = prefs.getString("widget_status", running ? "running" : "idle");
        String message = prefs.getString("widget_message", running ? "Bot service is enabled" : "No bot run yet");
        String ordersJson = prefs.getString("widget_orders", "[]");
        String statsJson = prefs.getString("widget_stats", "{}");
        long updatedAt = prefs.getLong("widget_updated_at", 0L);
        String updated = updatedAt > 0L
            ? new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date(updatedAt))
            : "-";
        MoneySummary moneySummary = moneySummary(statsJson);
        ProfitSummary profitSummary = profitSummary(statsJson);
        String ordersLine = orderLine(ordersJson);
        String positionsLine = positionsLine(statsJson);
        Bitmap chart = chartBitmap(statsJson);

        Intent open = new Intent(context, MainActivity.class);
        PendingIntent openIntent = PendingIntent.getActivity(context, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        for (int id : appWidgetIds) {
            RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_quant_bot);
            views.setOnClickPendingIntent(R.id.widget_root, openIntent);
            views.setTextViewText(R.id.widget_status, statusLine(running, status));
            views.setTextColor(R.id.widget_status, colorForStatus(running, status));
            views.setTextViewText(R.id.widget_message, message == null || message.isEmpty() ? "-" : message);
            views.setTextViewText(R.id.widget_updated, "Guncelleme " + updated);
            views.setTextViewText(R.id.widget_equity, "$" + moneySummary.equity);
            views.setTextViewText(R.id.widget_pl, profitSummary.line);
            views.setTextColor(R.id.widget_pl, profitSummary.positive ? Color.rgb(52, 211, 153) : Color.rgb(248, 113, 113));
            views.setTextViewText(R.id.widget_cash, "Nakit\n$" + moneySummary.cash);
            views.setTextViewText(R.id.widget_buying_power, "Alim gucu\n$" + moneySummary.buyingPower);
            views.setImageViewBitmap(R.id.widget_chart, chart);
            views.setTextViewText(R.id.widget_orders, ordersLine);
            views.setTextViewText(R.id.widget_positions, positionsLine);
            manager.updateAppWidget(id, views);
        }
    }

    private static MoneySummary moneySummary(String statsJson) {
        try {
            JSONObject stats = new JSONObject(statsJson == null ? "{}" : statsJson);
            double cash = stats.optDouble("cash", Double.NaN);
            double buyingPower = stats.optDouble("buying_power", Double.NaN);
            double equity = stats.optDouble("equity", Double.NaN);
            return new MoneySummary(money(cash), money(buyingPower), money(equity));
        } catch (Exception ignored) {
            return new MoneySummary("-", "-", "-");
        }
    }

    private static ProfitSummary profitSummary(String statsJson) {
        try {
            JSONObject stats = new JSONObject(statsJson == null ? "{}" : statsJson);
            double daily = stats.optDouble("daily_pl", 0.0);
            double weekly = stats.optDouble("weekly_pl", 0.0);
            double monthly = stats.optDouble("monthly_pl", 0.0);
            String line = "G " + signedMoney(daily) + "   H " + signedMoney(weekly) + "   A " + signedMoney(monthly);
            return new ProfitSummary(line, daily >= 0.0);
        } catch (Exception ignored) {
            return new ProfitSummary("G -   H -   A -", true);
        }
    }

    private static String orderLine(String ordersJson) {
        try {
            JSONArray orders = new JSONArray(ordersJson == null ? "[]" : ordersJson);
            if (orders.length() == 0) return "EMIRLER\nYok";
            StringBuilder text = new StringBuilder("EMIRLER");
            int limit = Math.min(4, orders.length());
            for (int i = 0; i < limit; i++) {
                JSONObject o = orders.optJSONObject(i);
                if (o == null) continue;
                text.append("\n");
                text.append(o.optString("symbol", "?")).append(" ")
                    .append(sideText(o.optString("side", "?"))).append(" ")
                    .append("$").append(money(orderNotional(o)));
                String state = o.optString("state", "");
                if (!state.isEmpty()) text.append(" - ").append(stateText(state));
            }
            if (orders.length() > limit) text.append("\n+").append(orders.length() - limit).append(" emir daha");
            return text.toString();
        } catch (Exception ignored) {
            return "EMIRLER\n-";
        }
    }

    private static String positionsLine(String statsJson) {
        try {
            JSONObject stats = new JSONObject(statsJson == null ? "{}" : statsJson);
            JSONArray positions = stats.optJSONArray("positions");
            if (positions == null || positions.length() == 0) return "POZISYONLAR\nYok";
            StringBuilder text = new StringBuilder("POZISYONLAR");
            int limit = Math.min(3, positions.length());
            for (int i = 0; i < limit; i++) {
                JSONObject p = positions.optJSONObject(i);
                if (p == null) continue;
                double pl = parseDouble(p.optString("unrealized_pl", "0"));
                text.append("\n")
                    .append(p.optString("symbol", "?")).append(" ")
                    .append(p.optString("qty", "?")).append(" lot ")
                    .append(pl >= 0 ? "+$" : "-$").append(money(Math.abs(pl)));
            }
            if (positions.length() > limit) text.append("\n+").append(positions.length() - limit).append(" pozisyon");
            return text.toString();
        } catch (Exception ignored) {
            return "POZISYONLAR\n-";
        }
    }

    private static Bitmap chartBitmap(String statsJson) {
        int width = 700;
        int height = 150;
        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        paint.setColor(Color.rgb(24, 34, 53));
        canvas.drawRect(0, 0, width, height, paint);
        try {
            JSONObject stats = new JSONObject(statsJson == null ? "{}" : statsJson);
            JSONObject history = stats.optJSONObject("portfolio_history");
            JSONArray equity = history == null ? null : history.optJSONArray("equity");
            ArrayList<Double> valid = validEquityValues(equity);
            double currentEquity = stats.optDouble("equity", 0.0);
            if (valid.size() == 0 && currentEquity > 0.0) valid.add(currentEquity);
            if (valid.size() < 2) {
                if (currentEquity > 0.0) return flatChart(bitmap, canvas, paint, currentEquity);
                return emptyChart(bitmap, canvas, paint, "Grafik verisi bekleniyor");
            }
            int start = Math.max(0, valid.size() - 22);
            double min = Double.MAX_VALUE;
            double max = -Double.MAX_VALUE;
            double[] values = new double[valid.size() - start];
            for (int i = start; i < valid.size(); i++) {
                double value = valid.get(i);
                values[i - start] = value;
                min = Math.min(min, value);
                max = Math.max(max, value);
            }
            double span = Math.max(1.0, max - min);
            int pad = 14;
            paint.setColor(Color.rgb(38, 54, 79));
            paint.setStrokeWidth(1.5f);
            canvas.drawLine(pad, height - pad, width - pad, height - pad, paint);
            canvas.drawLine(pad, height / 2f, width - pad, height / 2f, paint);
            Path path = new Path();
            Path fill = new Path();
            for (int i = 0; i < values.length; i++) {
                float x = pad + (width - 2f * pad) * i / Math.max(1, values.length - 1);
                float y = (float) (height - pad - ((values[i] - min) / span) * (height - 2f * pad));
                if (i == 0) path.moveTo(x, y); else path.lineTo(x, y);
                if (i == 0) fill.moveTo(x, height - pad);
                fill.lineTo(x, y);
            }
            fill.lineTo(width - pad, height - pad);
            fill.close();
            paint.setStyle(Paint.Style.FILL);
            paint.setColor(values[values.length - 1] >= values[0] ? Color.argb(48, 52, 211, 153) : Color.argb(48, 248, 113, 113));
            canvas.drawPath(fill, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(5f);
            paint.setColor(values[values.length - 1] >= values[0] ? Color.rgb(52, 211, 153) : Color.rgb(248, 113, 113));
            canvas.drawPath(path, paint);
            paint.setStyle(Paint.Style.FILL);
            float lastX = width - pad;
            float lastY = (float) (height - pad - ((values[values.length - 1] - min) / span) * (height - 2f * pad));
            canvas.drawCircle(lastX, lastY, 7f, paint);
            return bitmap;
        } catch (Exception ignored) {
            return emptyChart(bitmap, canvas, paint, "Grafik okunamadi");
        }
    }

    private static ArrayList<Double> validEquityValues(JSONArray equity) {
        ArrayList<Double> values = new ArrayList<>();
        if (equity == null) return values;
        for (int i = 0; i < equity.length(); i++) {
            double value = parseDouble(equity.optString(i, "0"));
            if (value > 1.0) values.add(value);
        }
        return values;
    }

    private static Bitmap flatChart(Bitmap bitmap, Canvas canvas, Paint paint, double equity) {
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(Color.rgb(24, 34, 53));
        canvas.drawRect(0, 0, bitmap.getWidth(), bitmap.getHeight(), paint);
        paint.setColor(Color.rgb(52, 211, 153));
        paint.setStrokeWidth(5f);
        int pad = 14;
        int y = bitmap.getHeight() / 2;
        canvas.drawLine(pad, y, bitmap.getWidth() - pad, y, paint);
        paint.setStyle(Paint.Style.FILL);
        canvas.drawCircle(bitmap.getWidth() - pad, y, 7f, paint);
        paint.setColor(Color.rgb(148, 163, 184));
        paint.setTextSize(22f);
        canvas.drawText("Baslangic $" + money(equity), 18, 34, paint);
        return bitmap;
    }

    private static Bitmap emptyChart(Bitmap bitmap, Canvas canvas, Paint paint, String text) {
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(Color.rgb(148, 163, 184));
        paint.setTextSize(24f);
        canvas.drawText(text, 18, 82, paint);
        return bitmap;
    }

    private static String statusLine(boolean running, String status) {
        String label = stateText(status);
        return running ? "AKTIF - " + label : "DURDU - " + label;
    }

    private static String sideText(String side) {
        if ("buy".equalsIgnoreCase(side)) return "AL";
        if ("sell".equalsIgnoreCase(side)) return "SAT";
        return side == null ? "?" : side.toUpperCase(Locale.US);
    }

    private static String stateText(String state) {
        if (state == null) return "-";
        String s = state.toLowerCase(Locale.US);
        if (s.contains("starting")) return "basliyor";
        if (s.contains("waiting") || s.contains("bekliyor")) return "bekliyor";
        if (s.contains("market") || s.contains("piyasa")) return "piyasa kapali";
        if (s.contains("pending") || s.contains("bekleyen")) return "bekleyen emir";
        if (s.contains("submitted") || s.contains("gonderildi")) return "emir gonderildi";
        if (s.contains("sync") || s.contains("senkron")) return "senkron";
        if (s.contains("blocked") || s.contains("bloklandi")) return "bloklandi";
        if (s.contains("error")) return "hata";
        if (s.contains("stopped")) return "durduruldu";
        if (s.contains("running")) return "calisiyor";
        return state;
    }

    private static String money(double value) {
        if (Double.isNaN(value)) return "-";
        return String.format(Locale.US, "%,.2f", value);
    }

    private static String signedMoney(double value) {
        if (Double.isNaN(value)) return "-";
        return (value >= 0.0 ? "+$" : "-$") + money(Math.abs(value));
    }

    private static double orderNotional(JSONObject o) {
        double notional = o.optDouble("notional", 0.0);
        if (notional > 0.0) return notional;
        double qty = parseDouble(o.optString("filled_qty", o.optString("qty", "0")));
        double price = o.optDouble("filled_avg_price", 0.0);
        return qty * price;
    }

    private static double parseDouble(String value) {
        try { return Double.parseDouble(value); } catch (Exception ignored) { return 0.0; }
    }

    private static int colorForStatus(boolean running, String status) {
        if (!running) return Color.rgb(180, 35, 24);
        String s = status == null ? "" : status.toLowerCase(Locale.US);
        if (s.contains("error") || s.contains("blocked") || s.contains("bloklandi")) return Color.rgb(248, 113, 113);
        if (s.contains("submitted") || s.contains("gonderildi") || s.contains("sync") || s.contains("senkron") || s.contains("running")) return Color.rgb(52, 211, 153);
        return Color.rgb(251, 191, 36);
    }

    static class MoneySummary {
        final String cash, buyingPower, equity;
        MoneySummary(String cash, String buyingPower, String equity) {
            this.cash = cash;
            this.buyingPower = buyingPower;
            this.equity = equity;
        }
    }

    static class ProfitSummary {
        final String line;
        final boolean positive;
        ProfitSummary(String line, boolean positive) {
            this.line = line;
            this.positive = positive;
        }
    }
}
