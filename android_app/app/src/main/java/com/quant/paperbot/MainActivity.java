package com.quant.paperbot;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.graphics.Color;

public class MainActivity extends Activity {
    private WebView webView;
    private SharedPreferences prefs;
    private final Handler refreshHandler = new Handler();
    private final Runnable refresher = new Runnable() {
        @Override public void run() {
            loadDashboard();
            refreshHandler.postDelayed(this, 30000);
        }
    };

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("quant_bot", MODE_PRIVATE);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 10);
        }
        buildUi();
        loadDashboard();
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(22, 22, 22, 22);
        root.setBackgroundColor(Color.rgb(15, 23, 42));

        TextView title = new TextView(this);
        title.setText("Pure Momentum 63");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        title.setPadding(0, 0, 0, 8);
        root.addView(title);

        TextView subtitle = new TextView(this);
        subtitle.setText("Pure momentum: 63g top 7, breadth cash, 1.8x paper hedef gross");
        subtitle.setTextColor(Color.rgb(203, 213, 225));
        subtitle.setTextSize(13);
        subtitle.setPadding(0, 0, 0, 12);
        root.addView(subtitle);

        EditText key = new EditText(this);
        key.setHint("Alpaca API anahtari");
        key.setSingleLine(true);
        key.setTextColor(Color.WHITE);
        key.setHintTextColor(Color.rgb(148, 163, 184));
        key.setText(prefs.getString("api_key", ""));
        root.addView(key);

        EditText secret = new EditText(this);
        secret.setHint("Alpaca gizli anahtar");
        secret.setSingleLine(true);
        secret.setTextColor(Color.WHITE);
        secret.setHintTextColor(Color.rgb(148, 163, 184));
        secret.setText(prefs.getString("secret_key", ""));
        root.addView(secret);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        Button saveStart = new Button(this);
        saveStart.setText("Kaydet ve Baslat");
        Button stop = new Button(this);
        stop.setText("Durdur");
        buttons.addView(saveStart, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        buttons.addView(stop, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        root.addView(buttons);

        webView = new WebView(this);
        webView.getSettings().setJavaScriptEnabled(true);
        root.addView(webView, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);

        saveStart.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                prefs.edit()
                    .putString("api_key", key.getText().toString().trim())
                    .putString("secret_key", secret.getText().toString().trim())
                    .putBoolean("running", true)
                    .putString("widget_status", "basliyor")
                    .putString("widget_message", "Bot servisi baslatiliyor")
                    .putString("widget_orders", "[]")
                    .putString("widget_stats", "{}")
                    .putLong("session_high_equity_bits", Double.doubleToLongBits(0.0))
                    .putLong("widget_updated_at", System.currentTimeMillis())
                    .apply();
                Intent intent = new Intent(MainActivity.this, BotService.class);
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(intent); else startService(intent);
                QuantBotWidgetProvider.updateAll(MainActivity.this);
                requestBatteryOptimizationBypass();
                loadDashboard();
            }
        });
        stop.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                prefs.edit()
                    .putBoolean("running", false)
                    .putString("widget_status", "durduruldu")
                    .putString("widget_message", "Bot servisi durduruldu")
                    .putString("widget_orders", "[]")
                    .putString("widget_stats", "{\"running\":false}")
                    .putLong("widget_updated_at", System.currentTimeMillis())
                    .apply();
                stopService(new Intent(MainActivity.this, BotService.class));
                QuantBotWidgetProvider.updateAll(MainActivity.this);
                loadDashboard();
            }
        });
    }

    private void requestBatteryOptimizationBypass() {
        if (Build.VERSION.SDK_INT < 23) return;
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm == null || pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        try {
            Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            intent.setData(Uri.parse("package:" + getPackageName()));
            startActivity(intent);
        } catch (Exception ignored) {
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        }
    }

    private void loadDashboard() {
        String html = prefs.getString("dashboard_html", DashboardRenderer.render("bekliyor", "Henuz bot calismadi", "[]", "{}"));
        webView.loadDataWithBaseURL("https://local.dashboard/", html, "text/html", "UTF-8", null);
    }

    @Override protected void onResume() {
        super.onResume();
        refreshHandler.post(refresher);
    }

    @Override protected void onPause() {
        refreshHandler.removeCallbacks(refresher);
        super.onPause();
    }
}
