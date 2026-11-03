package com.ailive.mobile;

import android.Manifest;
import android.app.Activity;
import android.app.Dialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Message;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.Locale;

public class MainActivity extends Activity {
    private static final String PREFS = "ailive_mobile";
    private static final String SERVER_URL = "server_url";
    private static final int MEDIA_PERMISSION_REQUEST = 100;

    private SharedPreferences preferences;
    private WebView webView;
    private EditText serverInput;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        preferences = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        showSetupOrConsole();
    }

    private void showSetupOrConsole() {
        String savedUrl = preferences.getString(SERVER_URL, "");
        if (savedUrl.isEmpty()) {
            showSetup();
        } else {
            loadConsole(normalizeUrl(savedUrl));
        }
    }

    private void showSetup() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        root.setPadding(dp(24), dp(36), dp(24), dp(24));
        root.setBackgroundColor(Color.rgb(244, 247, 252));

        TextView title = new TextView(this);
        title.setText("AI 数字人直播");
        title.setTextColor(Color.rgb(17, 28, 53));
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        title.setTypeface(null, 1);
        root.addView(title, matchWrap());

        TextView hint = new TextView(this);
        hint.setText("输入电脑上 LiveTalking 服务的地址。手机和电脑需要连接同一 Wi-Fi。");
        hint.setTextColor(Color.rgb(71, 85, 105));
        hint.setTextSize(15);
        hint.setGravity(Gravity.CENTER);
        hint.setPadding(0, dp(12), 0, dp(20));
        root.addView(hint, matchWrap());

        serverInput = new EditText(this);
        serverInput.setSingleLine(true);
        serverInput.setHint("例如：http://192.168.1.104:8010");
        serverInput.setText(preferences.getString(SERVER_URL, ""));
        serverInput.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        root.addView(serverInput, matchWrap());

        Button connect = new Button(this);
        connect.setText("连接直播工作台");
        connect.setTextSize(16);
        connect.setOnClickListener(view -> {
            String url = normalizeUrl(serverInput.getText().toString());
            if (url.isEmpty()) {
                serverInput.setError("请输入服务地址");
                return;
            }
            preferences.edit().putString(SERVER_URL, url).apply();
            loadConsole(url);
        });
        LinearLayout.LayoutParams buttonParams = matchWrap();
        buttonParams.topMargin = dp(16);
        root.addView(connect, buttonParams);

        TextView note = new TextView(this);
        note.setText("服务地址可在电脑启动窗口或 PC 工作台顶部查看。");
        note.setTextColor(Color.rgb(100, 116, 139));
        note.setTextSize(13);
        note.setGravity(Gravity.CENTER);
        note.setPadding(0, dp(18), 0, 0);
        root.addView(note, matchWrap());

        setContentView(root);
    }

    private void loadConsole(String url) {
        webView = createWebView(null);
        setContentView(webView);
        webView.loadUrl(url + "/live_console.html");
    }

    private WebView createWebView(Dialog owner) {
        WebView view = new WebView(this);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setSupportMultipleWindows(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setUserAgentString(settings.getUserAgentString() + " LiveTalkingMobile/1.0");

        view.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showSetup();
                    serverInput.setError("无法连接服务，请检查电脑和手机是否在同一 Wi-Fi");
                }
            }
        });
        view.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    if (hasMediaPermissions()) {
                        request.grant(request.getResources());
                    } else {
                        pendingPermissionRequest = request;
                        requestMediaPermissions();
                    }
                });
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture, Message resultMsg) {
                Dialog popupDialog = new Dialog(MainActivity.this,
                        android.R.style.Theme_DeviceDefault_NoActionBar_Fullscreen);
                WebView popup = createWebView(popupDialog);
                popupDialog.setContentView(popup);
                popupDialog.setOnDismissListener(dialog -> popup.destroy());
                popupDialog.show();
                WebView.WebViewTransport transport = (WebView.WebViewTransport) resultMsg.obj;
                transport.setWebView(popup);
                resultMsg.sendToTarget();
                return true;
            }

            @Override
            public void onCloseWindow(WebView window) {
                if (owner != null) {
                    owner.dismiss();
                }
            }
        });
        return view;
    }

    private PermissionRequest pendingPermissionRequest;

    private boolean hasMediaPermissions() {
        return checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private void requestMediaPermissions() {
        requestPermissions(
                new String[]{Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO},
                MEDIA_PERMISSION_REQUEST
        );
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == MEDIA_PERMISSION_REQUEST && pendingPermissionRequest != null) {
            if (hasMediaPermissions()) {
                pendingPermissionRequest.grant(pendingPermissionRequest.getResources());
            } else {
                pendingPermissionRequest.deny();
            }
            pendingPermissionRequest = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private String normalizeUrl(String value) {
        String url = value == null ? "" : value.trim();
        if (!url.isEmpty() && !url.contains("://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        if (url.toLowerCase(Locale.ROOT).endsWith("/live_console.html")) {
            url = url.substring(0, url.length() - "/live_console.html".length());
        }
        return url;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
