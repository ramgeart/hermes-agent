package ai.hermes.android

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Hub
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.SmartToy
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.TimeUnit

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val viewModel = ViewModelProvider(
            this,
            ViewModelProvider.AndroidViewModelFactory.getInstance(application)
        )[HermesViewModel::class.java]

        setContent { HermesAndroidTheme { HermesApp(viewModel) } }
    }
}

enum class Screen { Chat, Models, Terminal }

data class HermesSettings(
    val apiBaseUrl: String = "http://10.0.2.2:8642",
    val dashboardBaseUrl: String = "http://100.x.y.z:9119",
    val apiKey: String = "",
    val dashboardToken: String = "",
    val preferTailnet: Boolean = true,
)

data class ChatMessage(
    val id: String = UUID.randomUUID().toString(),
    val role: Role,
    val content: String,
) { enum class Role { User, Assistant, System, Error } }

data class ProviderOption(
    val slug: String,
    val label: String,
    val authStatus: String,
    val isCurrent: Boolean,
    val models: List<String>,
)

data class ModelInfo(
    val provider: String = "",
    val model: String = "",
    val effectiveContext: Int = 0,
)

data class HermesUiState(
    val screen: Screen = Screen.Chat,
    val settings: HermesSettings = HermesSettings(),
    val sessionId: String? = null,
    val messages: List<ChatMessage> = listOf(ChatMessage(role = ChatMessage.Role.System, content = "Cliente móvil para Hermes Agent: chat, modelos y terminal del dashboard.")),
    val input: String = "",
    val isBusy: Boolean = false,
    val status: String = "Sin verificar",
    val modelInfo: ModelInfo = ModelInfo(),
    val providers: List<ProviderOption> = emptyList(),
    val selectedProviderSlug: String = "",
    val modelFilter: String = "",
    val terminalConnected: Boolean = false,
    val terminalLog: String = "Terminal no conectado. Conecta al dashboard con /api/pty habilitado.",
    val terminalInput: String = "",
)

class HermesViewModel(application: Application) : AndroidViewModel(application) {
    private val store = SettingsStore(application)
    private val apiClient = HermesApiClient()
    private val dashboardClient = DashboardClient()
    private var terminalSocket: WebSocket? = null

    private val _uiState = MutableStateFlow(HermesUiState(settings = store.load()))
    val uiState: StateFlow<HermesUiState> = _uiState.asStateFlow()

    fun setScreen(screen: Screen) {
        _uiState.update { it.copy(screen = screen) }
        if (screen == Screen.Models && _uiState.value.providers.isEmpty()) refreshModels()
    }

    fun updateInput(value: String) = _uiState.update { it.copy(input = value) }
    fun updateTerminalInput(value: String) = _uiState.update { it.copy(terminalInput = value) }
    fun selectProvider(slug: String) = _uiState.update { it.copy(selectedProviderSlug = slug, modelFilter = "") }
    fun updateModelFilter(value: String) = _uiState.update { it.copy(modelFilter = value) }

    fun saveSettings(apiBaseUrl: String, dashboardBaseUrl: String, apiKey: String, dashboardToken: String, preferTailnet: Boolean) {
        val settings = HermesSettings(
            apiBaseUrl = apiBaseUrl.trim().trimEnd('/').ifBlank { HermesSettings().apiBaseUrl },
            dashboardBaseUrl = dashboardBaseUrl.trim().trimEnd('/').ifBlank { HermesSettings().dashboardBaseUrl },
            apiKey = apiKey.trim(),
            dashboardToken = dashboardToken.trim(),
            preferTailnet = preferTailnet,
        )
        store.save(settings)
        _uiState.update { it.copy(settings = settings, status = "Configuración guardada") }
    }

    fun checkHealth() {
        runBusy("Comprobando API Server…") { state ->
            val api = apiClient.getHealth(state.settings)
            val dashboard = dashboardClient.getStatus(state.settings)
            state.copy(status = "API: $api · Dashboard: $dashboard")
        }
    }

    fun newSession() {
        runBusy("Creando sesión…") { state ->
            val sessionId = apiClient.createSession(state.settings, title = "Hermes Android ${System.currentTimeMillis()}")
            state.copy(
                sessionId = sessionId,
                status = "Sesión $sessionId",
                messages = listOf(ChatMessage(role = ChatMessage.Role.System, content = "Nueva sesión Hermes: $sessionId")),
            )
        }
    }

    fun sendMessage() {
        val text = _uiState.value.input.trim()
        if (text.isEmpty() || _uiState.value.isBusy) return
        _uiState.update {
            it.copy(
                input = "",
                messages = it.messages + ChatMessage(role = ChatMessage.Role.User, content = text),
                isBusy = true,
                status = "Hermes está pensando…",
            )
        }
        viewModelScope.launch {
            val current = _uiState.value
            try {
                val sessionId = current.sessionId ?: apiClient.createSession(current.settings, title = "Hermes Android ${System.currentTimeMillis()}")
                val answer = apiClient.chat(current.settings, sessionId, text)
                _uiState.update {
                    it.copy(
                        sessionId = sessionId,
                        messages = it.messages + ChatMessage(role = ChatMessage.Role.Assistant, content = answer),
                        isBusy = false,
                        status = "Sesión $sessionId",
                    )
                }
            } catch (t: Throwable) { appendError(t) }
        }
    }

    fun refreshModels() {
        runBusy("Cargando modelos del dashboard…") { state ->
            val info = dashboardClient.getModelInfo(state.settings)
            val providers = dashboardClient.getModelOptions(state.settings)
            val selected = providers.firstOrNull { it.isCurrent }?.slug ?: providers.firstOrNull()?.slug.orEmpty()
            state.copy(modelInfo = info, providers = providers, selectedProviderSlug = selected, status = "Modelos cargados")
        }
    }

    fun setMainModel(provider: ProviderOption, model: String) {
        runBusy("Aplicando ${provider.slug}:$model…") { state ->
            dashboardClient.setMainModel(state.settings, provider.slug, model)
            val info = dashboardClient.getModelInfo(state.settings)
            state.copy(modelInfo = info, status = "Modelo principal actualizado")
        }
    }

    fun connectTerminal() {
        if (_uiState.value.terminalConnected) return
        val settings = _uiState.value.settings
        _uiState.update { it.copy(terminalLog = it.terminalLog + "\nConectando a /api/pty…", status = "Conectando terminal…") }
        terminalSocket = dashboardClient.openPty(
            settings = settings,
            onText = { text -> _uiState.update { it.copy(terminalLog = trimTerminal(it.terminalLog + text)) } },
            onOpen = { _uiState.update { it.copy(terminalConnected = true, status = "Terminal conectado", terminalLog = trimTerminal(it.terminalLog + "\n[conectado]\n")) } },
            onClosed = { reason -> _uiState.update { it.copy(terminalConnected = false, status = "Terminal cerrado", terminalLog = trimTerminal(it.terminalLog + "\n[cerrado] $reason\n")) } },
            onError = { err -> _uiState.update { it.copy(terminalConnected = false, status = "Error terminal", terminalLog = trimTerminal(it.terminalLog + "\n[error] $err\n")) } },
        )
    }

    fun disconnectTerminal() {
        terminalSocket?.close(1000, "client disconnect")
        terminalSocket = null
        _uiState.update { it.copy(terminalConnected = false, status = "Terminal desconectado") }
    }

    fun sendTerminalLine() {
        val line = _uiState.value.terminalInput
        if (line.isBlank()) return
        terminalSocket?.send(line + "\r")
        _uiState.update { it.copy(terminalInput = "") }
    }

    override fun onCleared() {
        terminalSocket?.close(1000, "viewmodel cleared")
    }

    private fun runBusy(initialStatus: String, block: suspend (HermesUiState) -> HermesUiState) {
        if (_uiState.value.isBusy) return
        _uiState.update { it.copy(isBusy = true, status = initialStatus) }
        viewModelScope.launch {
            try { _uiState.value = block(_uiState.value).copy(isBusy = false) }
            catch (t: Throwable) { appendError(t) }
        }
    }

    private fun appendError(t: Throwable) {
        _uiState.update {
            it.copy(
                isBusy = false,
                status = "Error",
                messages = it.messages + ChatMessage(role = ChatMessage.Role.Error, content = t.message ?: "Error desconocido"),
            )
        }
    }

    private fun trimTerminal(text: String): String = if (text.length > 40_000) text.takeLast(40_000) else text
}

class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("hermes_android", Context.MODE_PRIVATE)
    fun load(): HermesSettings = HermesSettings(
        apiBaseUrl = prefs.getString(KEY_API_URL, null) ?: HermesSettings().apiBaseUrl,
        dashboardBaseUrl = prefs.getString(KEY_DASHBOARD_URL, null) ?: HermesSettings().dashboardBaseUrl,
        apiKey = prefs.getString(KEY_API_KEY, null) ?: "",
        dashboardToken = prefs.getString(KEY_DASHBOARD_TOKEN, null) ?: "",
        preferTailnet = prefs.getBoolean(KEY_TAILNET, true),
    )
    fun save(settings: HermesSettings) {
        prefs.edit()
            .putString(KEY_API_URL, settings.apiBaseUrl)
            .putString(KEY_DASHBOARD_URL, settings.dashboardBaseUrl)
            .putString(KEY_API_KEY, settings.apiKey)
            .putString(KEY_DASHBOARD_TOKEN, settings.dashboardToken)
            .putBoolean(KEY_TAILNET, settings.preferTailnet)
            .apply()
    }
    private companion object {
        const val KEY_API_URL = "api_url"
        const val KEY_DASHBOARD_URL = "dashboard_url"
        const val KEY_API_KEY = "api_key"
        const val KEY_DASHBOARD_TOKEN = "dashboard_token"
        const val KEY_TAILNET = "tailnet"
    }
}

class HermesApiClient {
    suspend fun getHealth(settings: HermesSettings): String = withContext(Dispatchers.IO) {
        JSONObject(request(settings.apiBaseUrl, "GET", "/health", bearer = settings.apiKey)).optString("status", "ok")
    }
    suspend fun createSession(settings: HermesSettings, title: String): String = withContext(Dispatchers.IO) {
        val body = request(settings.apiBaseUrl, "POST", "/api/sessions", JSONObject().put("title", title).toString(), bearer = settings.apiKey)
        JSONObject(body).getJSONObject("session").getString("id")
    }
    suspend fun chat(settings: HermesSettings, sessionId: String, message: String): String = withContext(Dispatchers.IO) {
        val id = URLEncoder.encode(sessionId, StandardCharsets.UTF_8.name())
        val body = request(settings.apiBaseUrl, "POST", "/api/sessions/$id/chat", JSONObject().put("message", message).toString(), bearer = settings.apiKey)
        JSONObject(body).getJSONObject("message").optString("content", "")
    }
}

class DashboardClient {
    private val okHttp = OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build()

    suspend fun getStatus(settings: HermesSettings): String = withContext(Dispatchers.IO) {
        val body = request(settings.dashboardBaseUrl, "GET", "/api/status", dashboardToken = settings.dashboardToken)
        val json = JSONObject(body)
        val gateway = if (json.optBoolean("gateway_running", false)) "gateway ok" else "gateway off"
        json.optString("version", "dashboard") + " / " + gateway
    }

    suspend fun getModelInfo(settings: HermesSettings): ModelInfo = withContext(Dispatchers.IO) {
        val json = JSONObject(request(settings.dashboardBaseUrl, "GET", "/api/model/info", dashboardToken = settings.dashboardToken))
        ModelInfo(
            provider = json.optString("provider", ""),
            model = json.optString("model", ""),
            effectiveContext = json.optInt("effective_context_length", 0),
        )
    }

    suspend fun getModelOptions(settings: HermesSettings): List<ProviderOption> = withContext(Dispatchers.IO) {
        val json = JSONObject(request(settings.dashboardBaseUrl, "GET", "/api/model/options", dashboardToken = settings.dashboardToken))
        val arr = json.optJSONArray("providers") ?: JSONArray()
        buildList {
            for (i in 0 until arr.length()) {
                val p = arr.optJSONObject(i) ?: continue
                val modelsJson = p.optJSONArray("models") ?: JSONArray()
                val models = buildList { for (j in 0 until modelsJson.length()) add(modelsJson.optString(j)) }.filter { it.isNotBlank() }
                add(
                    ProviderOption(
                        slug = p.optString("slug", p.optString("provider", "")),
                        label = p.optString("label", p.optString("name", p.optString("slug", "provider"))),
                        authStatus = p.optString("auth_status", p.optString("status", "")),
                        isCurrent = p.optBoolean("is_current", false),
                        models = models,
                    )
                )
            }
        }
    }

    suspend fun setMainModel(settings: HermesSettings, provider: String, model: String) = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("scope", "main").put("provider", provider).put("model", model).toString()
        request(settings.dashboardBaseUrl, "POST", "/api/model/set", payload, dashboardToken = settings.dashboardToken)
        Unit
    }

    fun openPty(
        settings: HermesSettings,
        onText: (String) -> Unit,
        onOpen: () -> Unit,
        onClosed: (String) -> Unit,
        onError: (String) -> Unit,
    ): WebSocket {
        val base = wsBase(settings.dashboardBaseUrl)
        val token = URLEncoder.encode(settings.dashboardToken, StandardCharsets.UTF_8.name())
        val url = "$base/api/pty?token=$token&channel=android-${UUID.randomUUID().toString().take(8)}"
        val req = Request.Builder().url(url).build()
        return okHttp.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send("\u001b[RESIZE:100;32]")
                onOpen()
            }
            override fun onMessage(webSocket: WebSocket, text: String) = onText(stripAnsi(text))
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) = onText(stripAnsi(bytes.string(StandardCharsets.UTF_8)))
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(code, reason) }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = onClosed("$code $reason")
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = onError(t.message ?: "WebSocket failure")
        })
    }
}

fun request(baseUrl: String, method: String, path: String, body: String? = null, bearer: String = "", dashboardToken: String = ""): String {
    val base = baseUrl.trim().trimEnd('/')
    require(base.startsWith("http://") || base.startsWith("https://")) { "URL inválida: $base" }
    val connection = (URL(base + path).openConnection() as HttpURLConnection).apply {
        requestMethod = method
        connectTimeout = 15_000
        readTimeout = 180_000
        setRequestProperty("Accept", "application/json")
        if (bearer.isNotBlank()) setRequestProperty("Authorization", "Bearer $bearer")
        if (dashboardToken.isNotBlank()) {
            setRequestProperty("X-Hermes-Session-Token", dashboardToken)
            setRequestProperty("Authorization", "Bearer $dashboardToken")
        }
        if (body != null) {
            doOutput = true
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
        }
    }
    if (body != null) OutputStreamWriter(connection.outputStream, StandardCharsets.UTF_8).use { it.write(body) }
    val status = connection.responseCode
    val stream = if (status in 200..299) connection.inputStream else connection.errorStream
    val text = stream?.use { BufferedReader(InputStreamReader(it, StandardCharsets.UTF_8)).readText() }.orEmpty()
    connection.disconnect()
    if (status !in 200..299) error("HTTP $status: ${extractError(text)}")
    return text
}

fun extractError(text: String): String = runCatching {
    val json = JSONObject(text)
    json.optString("detail", json.optJSONObject("error")?.optString("message") ?: text)
}.getOrDefault(text)

fun wsBase(httpBase: String): String = httpBase.trim().trimEnd('/').let {
    when {
        it.startsWith("https://") -> "wss://" + it.removePrefix("https://")
        it.startsWith("http://") -> "ws://" + it.removePrefix("http://")
        else -> error("URL inválida: $it")
    }
}

fun stripAnsi(text: String): String = text
    .replace(Regex("\\u001B\\[[;?0-9]*[ -/]*[@-~]"), "")
    .replace("\r", "")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HermesApp(viewModel: HermesViewModel) {
    val state by viewModel.uiState.collectAsStateCompat()
    var showSettings by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Hermes Agent")
                        Text(state.status, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                },
                actions = {
                    if (state.isBusy) CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                    IconButton(onClick = viewModel::newSession, enabled = !state.isBusy) { Icon(Icons.Default.Add, "Nueva sesión") }
                    IconButton(onClick = { showSettings = true }) { Icon(Icons.Default.Settings, "Configuración") }
                }
            )
        },
        bottomBar = {
            Column {
                if (state.screen == Screen.Chat) MessageInput(state.input, !state.isBusy, viewModel::updateInput, viewModel::sendMessage)
                NavigationBar {
                    NavigationBarItem(selected = state.screen == Screen.Chat, onClick = { viewModel.setScreen(Screen.Chat) }, icon = { Icon(Icons.Default.SmartToy, null) }, label = { Text("Chat") })
                    NavigationBarItem(selected = state.screen == Screen.Models, onClick = { viewModel.setScreen(Screen.Models) }, icon = { Icon(Icons.Default.Hub, null) }, label = { Text("Modelos") })
                    NavigationBarItem(selected = state.screen == Screen.Terminal, onClick = { viewModel.setScreen(Screen.Terminal) }, icon = { Icon(Icons.Default.Computer, null) }, label = { Text("Terminal") })
                }
            }
        }
    ) { padding ->
        Box(modifier = Modifier.fillMaxSize().padding(padding)) {
            when (state.screen) {
                Screen.Chat -> ChatScreen(state)
                Screen.Models -> ModelsScreen(state, viewModel)
                Screen.Terminal -> TerminalScreen(state, viewModel)
            }
        }
    }
    if (showSettings) SettingsDialog(state.settings, state.isBusy, { showSettings = false }, viewModel::saveSettings, viewModel::checkHealth)
}

@Composable
fun ChatScreen(state: HermesUiState) {
    val listState = rememberLazyListState()
    LaunchedEffect(state.messages.size) { if (state.messages.isNotEmpty()) listState.animateScrollToItem(state.messages.lastIndex) }
    LazyColumn(modifier = Modifier.fillMaxSize().padding(12.dp), state = listState, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        items(state.messages, key = { it.id }) { MessageBubble(it) }
    }
}

@Composable
fun ModelsScreen(state: HermesUiState, vm: HermesViewModel) {
    val selected = state.providers.firstOrNull { it.slug == state.selectedProviderSlug } ?: state.providers.firstOrNull()
    val filteredModels = selected?.models.orEmpty().filter { it.contains(state.modelFilter, ignoreCase = true) }.take(80)
    LazyColumn(modifier = Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            Card { Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Modelo actual", fontWeight = FontWeight.Bold)
                Text("${state.modelInfo.provider} / ${state.modelInfo.model}")
                if (state.modelInfo.effectiveContext > 0) Text("Contexto efectivo: ${state.modelInfo.effectiveContext}")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = vm::refreshModels, enabled = !state.isBusy) { Text("Refrescar") }
                }
            } }
        }
        item { Text("Proveedores", style = MaterialTheme.typography.titleMedium) }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                state.providers.take(4).forEach { p ->
                    FilterChip(
                        selected = p.slug == selected?.slug,
                        onClick = { vm.selectProvider(p.slug) },
                        label = { Text(p.label.take(12)) },
                        leadingIcon = if (p.isCurrent) ({ Icon(Icons.Default.CheckCircle, null, Modifier.size(16.dp)) }) else null,
                    )
                }
            }
        }
        item {
            OutlinedTextField(
                value = state.modelFilter,
                onValueChange = vm::updateModelFilter,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("Filtrar modelos de ${selected?.label ?: "proveedor"}") },
            )
        }
        items(filteredModels) { model ->
            Card(
                modifier = Modifier.fillMaxWidth().clickable(enabled = selected != null && !state.isBusy) { selected?.let { vm.setMainModel(it, model) } },
                colors = CardDefaults.cardColors(containerColor = if (model == state.modelInfo.model) Color(0xFFE0F2FE) else MaterialTheme.colorScheme.surfaceVariant),
            ) { Column(Modifier.padding(12.dp)) {
                Text(model, fontWeight = FontWeight.SemiBold)
                Text("Tocar para aplicar como modelo principal", style = MaterialTheme.typography.labelSmall)
            } }
        }
        if (state.providers.size > 4) {
            item { Text("Otros proveedores: ${state.providers.drop(4).joinToString { it.label }}", style = MaterialTheme.typography.bodySmall) }
        }
    }
}

@Composable
fun TerminalScreen(state: HermesUiState, vm: HermesViewModel) {
    val listState = rememberLazyListState()
    val lines = state.terminalLog.lines().takeLast(900)
    LaunchedEffect(lines.size, state.terminalLog.length) { if (lines.isNotEmpty()) listState.animateScrollToItem(lines.lastIndex) }
    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = vm::connectTerminal, enabled = !state.terminalConnected) { Text("Conectar") }
            Button(onClick = vm::disconnectTerminal, enabled = state.terminalConnected) { Text("Desconectar") }
        }
        LazyColumn(
            state = listState,
            modifier = Modifier.weight(1f).fillMaxWidth().background(Color(0xFF0B1020), RoundedCornerShape(12.dp)).padding(12.dp),
        ) { items(lines) { Text(it.ifBlank { " " }, color = Color(0xFFE5E7EB), fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall) } }
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = state.terminalInput,
                onValueChange = vm::updateTerminalInput,
                enabled = state.terminalConnected,
                modifier = Modifier.weight(1f),
                label = { Text("Comando / entrada PTY") },
                singleLine = true,
            )
            Spacer(Modifier.width(8.dp))
            Button(onClick = vm::sendTerminalLine, enabled = state.terminalConnected && state.terminalInput.isNotBlank()) { Text("Enter") }
        }
    }
}

@Composable
fun MessageInput(value: String, enabled: Boolean, onValueChange: (String) -> Unit, onSend: () -> Unit) {
    Surface(shadowElevation = 8.dp) {
        Row(modifier = Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(modifier = Modifier.weight(1f), value = value, onValueChange = onValueChange, enabled = enabled, placeholder = { Text("Escribe a Hermes…") }, minLines = 1, maxLines = 5)
            Spacer(Modifier.width(8.dp))
            Button(onClick = onSend, enabled = enabled && value.isNotBlank()) { Icon(Icons.Default.Send, "Enviar") }
        }
    }
}

@Composable
fun MessageBubble(message: ChatMessage) {
    val isUser = message.role == ChatMessage.Role.User
    val bubbleColor = when (message.role) {
        ChatMessage.Role.User -> MaterialTheme.colorScheme.primary
        ChatMessage.Role.Assistant -> MaterialTheme.colorScheme.surfaceVariant
        ChatMessage.Role.System -> Color(0xFFE0F2FE)
        ChatMessage.Role.Error -> Color(0xFFFEE2E2)
    }
    val textColor = when (message.role) {
        ChatMessage.Role.User -> MaterialTheme.colorScheme.onPrimary
        ChatMessage.Role.Assistant -> MaterialTheme.colorScheme.onSurfaceVariant
        ChatMessage.Role.System -> Color(0xFF075985)
        ChatMessage.Role.Error -> Color(0xFF991B1B)
    }
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start) {
        Column(modifier = Modifier.fillMaxWidth(0.88f).background(bubbleColor, RoundedCornerShape(18.dp)).padding(14.dp)) {
            Text(when (message.role) { ChatMessage.Role.User -> "Tú"; ChatMessage.Role.Assistant -> "Hermes"; ChatMessage.Role.System -> "Sistema"; ChatMessage.Role.Error -> "Error" }, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold, color = textColor)
            Spacer(Modifier.height(4.dp)); Text(message.content, color = textColor)
        }
    }
}

@Composable
fun SettingsDialog(
    settings: HermesSettings,
    isBusy: Boolean,
    onDismiss: () -> Unit,
    onSave: (String, String, String, String, Boolean) -> Unit,
    onCheck: () -> Unit,
) {
    val context = LocalContext.current
    var apiUrl by remember(settings.apiBaseUrl) { mutableStateOf(settings.apiBaseUrl) }
    var dashUrl by remember(settings.dashboardBaseUrl) { mutableStateOf(settings.dashboardBaseUrl) }
    var apiKey by remember(settings.apiKey) { mutableStateOf(settings.apiKey) }
    var dashToken by remember(settings.dashboardToken) { mutableStateOf(settings.dashboardToken) }
    var tailnet by remember(settings.preferTailnet) { mutableStateOf(settings.preferTailnet) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Conectar con Hermes") },
        text = {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                item { Text("Para Tailnet, activa Tailscale en Android y usa el nombre/IP 100.x del nodo. La app usa HTTP/WebSocket normal sobre la VPN de Tailnet.") }
                item { FilterChip(selected = tailnet, onClick = { tailnet = !tailnet }, label = { Text("Usar Tailnet/VPN") }) }
                item { OutlinedTextField(value = apiUrl, onValueChange = { apiUrl = it }, label = { Text("API Server URL (:8642)") }, singleLine = true, modifier = Modifier.fillMaxWidth()) }
                item { OutlinedTextField(value = dashUrl, onValueChange = { dashUrl = it }, label = { Text("Dashboard URL (:9119)") }, singleLine = true, modifier = Modifier.fillMaxWidth()) }
                item { OutlinedTextField(value = apiKey, onValueChange = { apiKey = it }, label = { Text("API_SERVER_KEY") }, singleLine = true, modifier = Modifier.fillMaxWidth()) }
                item { OutlinedTextField(value = dashToken, onValueChange = { dashToken = it }, label = { Text("Dashboard session token") }, singleLine = true, modifier = Modifier.fillMaxWidth()) }
                item { TextButton(onClick = { runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("tailscale://"))) } }) { Text("Abrir Tailscale") } }
            }
        },
        confirmButton = { Button(onClick = { onSave(apiUrl, dashUrl, apiKey, dashToken, tailnet) }, enabled = !isBusy) { Text("Guardar") } },
        dismissButton = { Row { TextButton(onClick = onCheck, enabled = !isBusy) { Text("Probar") }; TextButton(onClick = onDismiss) { Text("Cancelar") } } },
    )
}

@Composable
fun HermesAndroidTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = androidx.compose.material3.lightColorScheme(
            primary = Color(0xFF6D28D9), secondary = Color(0xFF0EA5E9), background = Color(0xFFF8FAFC), surface = Color.White, surfaceVariant = Color(0xFFEDE9FE)
        ),
        content = content,
    )
}

@Composable
private fun <T> StateFlow<T>.collectAsStateCompat(): State<T> = collectAsState(value)
