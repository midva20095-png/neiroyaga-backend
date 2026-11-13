package ru.babayagaland.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Message
import android.provider.MediaStore
import android.view.View
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.net.http.SslError
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import java.io.File

class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var progress: ProgressBar
    private lateinit var offline: View
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var cameraUri: Uri? = null
    private var pendingDownload: DownloadRequest? = null
    private var pageFailed = false

    private val fileChooser = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val callback = fileCallback
        fileCallback = null
        if (callback == null) return@registerForActivityResult
        val uris = mutableListOf<Uri>()
        if (result.resultCode == Activity.RESULT_OK) {
            val data = result.data
            val clip = data?.clipData
            when {
                clip != null -> {
                    for (i in 0 until clip.itemCount) uris += clip.getItemAt(i).uri
                }
                data?.data != null -> uris += data.data!!
                cameraUri != null -> uris += cameraUri!!
            }
        }
        callback.onReceiveValue(if (uris.isEmpty()) null else uris.toTypedArray())
        cameraUri = null
    }

    private val storagePermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val request = pendingDownload
        pendingDownload = null
        if (granted && request != null) enqueueDownload(request) else if (request != null) {
            toast(R.string.download_failed)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        try {
            setContentView(R.layout.activity_main)
        } catch (_: Throwable) {
            showMissingWebView()
            return
        }

        web = findViewById(R.id.web)
        progress = findViewById(R.id.progress)
        offline = findViewById(R.id.offline)
        keepPageInsideScreen()
        findViewById<View>(R.id.retry).setOnClickListener { reloadHome() }
        findViewById<View>(R.id.browser).setOnClickListener { openInBrowser() }
        val messengers = findViewById<View>(R.id.messengers)
        if (resources.getBoolean(R.bool.show_messengers)) {
            messengers.visibility = View.VISIBLE
            findViewById<View>(R.id.open_vk).setOnClickListener { openExternal(getString(R.string.vk_bot_url)) }
            findViewById<View>(R.id.open_max).setOnClickListener { openExternal(getString(R.string.max_bot_url)) }
        }

        try {
            configureWeb(savedInstanceState)
        } catch (_: Throwable) {
            showMissingWebView()
            return
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (offline.visibility == View.VISIBLE) {
                    offline.visibility = View.GONE
                    return
                }
                if (web.canGoBack()) web.goBack() else finish()
            }
        })
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWeb(savedInstanceState: Bundle?) {
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            javaScriptCanOpenWindowsAutomatically = true
            setSupportMultipleWindows(true)
            loadsImagesAutomatically = true
            useWideViewPort = true
            loadWithOverviewMode = false
            mediaPlaybackRequiresUserGesture = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            allowFileAccess = false
            allowContentAccess = true
            cacheMode = WebSettings.LOAD_DEFAULT
            // Метка «wv» и свой суффикс заставляют DDoS-Guard обрывать адрес на Samsung.
            userAgentString = browserUserAgent(userAgentString)
            loadWithOverviewMode = resources.configuration.smallestScreenWidthDp >= 600
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                safeBrowsingEnabled = false
            }
        }
        web.isVerticalScrollBarEnabled = true
        web.overScrollMode = View.OVER_SCROLL_IF_CONTENT_SCROLLS

        web.webViewClient = SiteClient()
        web.webChromeClient = SiteChrome()
        web.setDownloadListener { url, userAgent, contentDisposition, mime, _ ->
            startDownload(url, userAgent, contentDisposition, mime)
        }

        web.post {
            if (savedInstanceState != null) {
                web.restoreState(savedInstanceState)
            } else {
                web.loadUrl(getString(R.string.home_url))
            }
        }
    }

    private fun browserUserAgent(raw: String?): String {
        val chrome = Regex("Chrome/([\\d.]+)").find(raw.orEmpty())?.groupValues?.getOrNull(1) ?: "120.0.0.0"
        val tablet = resources.configuration.smallestScreenWidthDp >= 600
        val mobile = if (tablet) "" else " Mobile"
        val model = Build.MODEL.orEmpty().replace(";", " ").ifBlank { "Android" }
        return "Mozilla/5.0 (Linux; Android ${Build.VERSION.RELEASE}; $model) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/$chrome$mobile Safari/537.36"
    }

    private fun reloadHome() {
        offline.visibility = View.GONE
        pageFailed = false
        try {
            web.loadUrl(getString(R.string.home_url))
        } catch (_: Throwable) {
            showFailure(WebViewClient.ERROR_UNKNOWN)
        }
    }

    private fun openInBrowser() {
        openExternal(getString(R.string.home_url))
    }

    private fun openExternal(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (_: ActivityNotFoundException) {
            toast(R.string.open_failed)
        }
    }

    private fun showFailure(code: Int) {
        val title = findViewById<TextView>(R.id.offline_title)
        val body = findViewById<TextView>(R.id.offline_body)
        when (code) {
            WebViewClient.ERROR_HOST_LOOKUP -> {
                title.setText(R.string.lookup_title)
                body.setText(R.string.lookup_body)
            }
            WebViewClient.ERROR_FAILED_SSL_HANDSHAKE -> {
                title.setText(R.string.ssl_title)
                body.setText(R.string.ssl_body)
            }
            else -> {
                title.setText(R.string.offline_title)
                body.setText(R.string.offline_body)
            }
        }
        offline.visibility = View.VISIBLE
    }

    private fun keepPageInsideScreen() {
        val root = findViewById<View>(R.id.root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }
        ViewCompat.requestApplyInsets(root)
    }

    private fun fitSiteMenu(view: WebView) {
        val js = """
            (function(){
              var old=document.getElementById('ny-fit');
              if(old) old.remove();
              var s=document.createElement('style');
              s.id='ny-fit';
              s.textContent='html,body{overflow-x:hidden!important;max-width:100%!important}#allrecords,.t-records{overflow-x:hidden!important;max-width:100vw!important}.t228,.t228__maincontainer,.t228__leftcontainer,.t228__rightcontainer,.t228__list,.t396__artboard{max-width:100vw!important;box-sizing:border-box!important}';
              (document.head||document.documentElement).appendChild(s);
            })();
        """.trimIndent()
        view.evaluateJavascript(js, null)
    }

    private fun showMissingWebView() {
        setContentView(TextView(this).apply {
            setText(R.string.webview_missing)
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#06030D"))
            textSize = 16f
            setPadding(48, 48, 48, 48)
        })
    }

    private fun shouldLeaveApp(uri: Uri): Boolean {
        val scheme = uri.scheme?.lowercase() ?: return true
        if (scheme != "http" && scheme != "https") return true
        val host = uri.host?.lowercase() ?: return false
        return host == "t.me" || host.endsWith(".t.me") ||
            host == "telegram.me" || host.endsWith(".telegram.me") ||
            host == "wa.me"
    }

    private fun openOutside(uri: Uri) {
        val scheme = uri.scheme?.lowercase().orEmpty()
        try {
            if (scheme == "intent") {
                val parsed = Intent.parseUri(uri.toString(), Intent.URI_INTENT_SCHEME)
                val fallback = parsed.getStringExtra("browser_fallback_url")
                if (parsed.resolveActivity(packageManager) != null) {
                    startActivity(parsed)
                } else if (!fallback.isNullOrBlank()) {
                    web.loadUrl(fallback)
                } else {
                    toast(R.string.open_failed)
                }
                return
            }
            val intent = if (scheme == "mailto" || scheme == "sms" || scheme == "smsto") {
                Intent(Intent.ACTION_SENDTO, uri)
            } else if (scheme == "tel") {
                Intent(Intent.ACTION_DIAL, uri)
            } else {
                Intent(Intent.ACTION_VIEW, uri)
            }
            startActivity(intent)
        } catch (_: ActivityNotFoundException) {
            toast(R.string.open_failed)
        } catch (_: Exception) {
            toast(R.string.open_failed)
        }
    }

    private fun route(uri: Uri): Boolean {
        if (!shouldLeaveApp(uri)) return false
        openOutside(uri)
        return true
    }

    private fun startDownload(url: String, userAgent: String, contentDisposition: String?, mime: String?) {
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            toast(R.string.download_unsupported)
            return
        }
        val request = DownloadRequest(url, userAgent, contentDisposition, mime)
        if (Build.VERSION.SDK_INT < 29 &&
            ContextCompat.checkSelfPermission(this, android.Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED
        ) {
            pendingDownload = request
            storagePermission.launch(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            return
        }
        enqueueDownload(request)
    }

    private fun enqueueDownload(request: DownloadRequest) {
        try {
            val name = URLUtil.guessFileName(request.url, request.contentDisposition, request.mime)
            val download = DownloadManager.Request(Uri.parse(request.url)).apply {
                addRequestHeader("User-Agent", request.userAgent)
                CookieManager.getInstance().getCookie(request.url)?.let { addRequestHeader("Cookie", it) }
                setTitle(name)
                setMimeType(request.mime)
                setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
            }
            getSystemService(DownloadManager::class.java).enqueue(download)
            toast(R.string.download_started)
        } catch (_: Exception) {
            toast(R.string.download_failed)
        }
    }

    private fun launchFileChooser(params: WebChromeClient.FileChooserParams?) {
        val accept = params?.acceptTypes?.filter { it.isNotBlank() }?.toTypedArray()
        val content = Intent(Intent.ACTION_GET_CONTENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = if (accept.isNullOrEmpty() || accept.size > 1) "*/*" else accept[0]
            if (!accept.isNullOrEmpty()) putExtra(Intent.EXTRA_MIME_TYPES, accept)
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params?.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE)
        }
        val extras = mutableListOf<Intent>()
        cameraUri = createCameraUri()
        val camera = cameraUri
        if (camera != null &&
            packageManager.resolveActivity(Intent(MediaStore.ACTION_IMAGE_CAPTURE), PackageManager.MATCH_DEFAULT_ONLY) != null &&
            (accept == null || accept.any { it == "*/*" || it.startsWith("image") || it.isBlank() })
        ) {
            extras += Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
                putExtra(MediaStore.EXTRA_OUTPUT, camera)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            }
        }
        val chooser = Intent.createChooser(content, getString(R.string.choose_file)).apply {
            if (extras.isNotEmpty()) putExtra(Intent.EXTRA_INITIAL_INTENTS, extras.toTypedArray())
        }
        try {
            fileChooser.launch(chooser)
        } catch (_: ActivityNotFoundException) {
            fileCallback?.onReceiveValue(null)
            fileCallback = null
            toast(R.string.open_failed)
        }
    }

    private fun createCameraUri(): Uri? {
        return try {
            val dir = File(cacheDir, "camera").apply { mkdirs() }
            val file = File(dir, "shot-${System.currentTimeMillis()}.jpg")
            FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        } catch (_: Exception) {
            null
        }
    }

    private fun toast(message: Int) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        if (::web.isInitialized) web.saveState(outState)
    }

    override fun onPause() {
        if (::web.isInitialized) web.onPause()
        try {
            CookieManager.getInstance().flush()
        } catch (_: Exception) {
        }
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) web.onResume()
    }

    override fun onDestroy() {
        fileCallback?.onReceiveValue(null)
        fileCallback = null
        if (::web.isInitialized) {
            web.stopLoading()
            web.webChromeClient = null
            web.destroy()
        }
        super.onDestroy()
    }

    private inner class SiteClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            return route(request.url)
        }

        override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
            pageFailed = false
            offline.visibility = View.GONE
        }

        override fun onPageFinished(view: WebView, url: String?) {
            if (url != null && (url.startsWith("chrome-error://") || url.contains("chromewebdata"))) {
                if (!pageFailed) {
                    pageFailed = true
                    showFailure(WebViewClient.ERROR_CONNECT)
                }
                return
            }
            if (!pageFailed) offline.visibility = View.GONE
            fitSiteMenu(view)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            val code = error.errorCode
            if (code == WebViewClient.ERROR_UNKNOWN) return
            pageFailed = true
            showFailure(code)
        }

        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
            handler.cancel()
            val current = view.url
            val failedMain = current.isNullOrBlank() || current == "about:blank" || current == error.url
            if (!failedMain) return
            pageFailed = true
            showFailure(WebViewClient.ERROR_FAILED_SSL_HANDSHAKE)
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            pageFailed = true
            showFailure(WebViewClient.ERROR_UNKNOWN)
            return true
        }
    }

    private inner class SiteChrome : WebChromeClient() {
        override fun onProgressChanged(view: WebView, newProgress: Int) {
            progress.progress = newProgress
            progress.visibility = if (newProgress in 1..99) View.VISIBLE else View.GONE
        }

        override fun onShowFileChooser(
            webView: WebView,
            filePathCallback: ValueCallback<Array<Uri>>,
            fileChooserParams: FileChooserParams
        ): Boolean {
            fileCallback?.onReceiveValue(null)
            fileCallback = filePathCallback
            launchFileChooser(fileChooserParams)
            return true
        }

        override fun onCreateWindow(view: WebView, isDialog: Boolean, isUserGesture: Boolean, resultMsg: Message): Boolean {
            val popup = WebView(this@MainActivity)
            popup.webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                    val uri = request.url
                    if (route(uri)) {
                        popup.destroy()
                        return true
                    }
                    web.loadUrl(uri.toString())
                    popup.destroy()
                    return true
                }
            }
            val transport = resultMsg.obj as WebView.WebViewTransport
            transport.webView = popup
            resultMsg.sendToTarget()
            return true
        }
    }

    private data class DownloadRequest(
        val url: String,
        val userAgent: String,
        val contentDisposition: String?,
        val mime: String?
    )
}
