package com.rixmerz.ryokumobile


import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridItemSpanScope
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.blur
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.ColorMatrix
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class LauncherActivity : ComponentActivity() {
    private var apps by mutableStateOf<List<AppEntry>>(emptyList())
    // Bumped on every HOME press so the UI returns to the home page.
    private var homeTick by mutableIntStateOf(0)
    // The wallpaper only animates while the launcher is on screen, to spare the battery.
    private var resumed by mutableStateOf(false)
    private val tweaks by lazy { SystemTweaks(this) }
    private val tasks by lazy { Tasks(this) }
    private val link = DesktopLink()

    // Refresh the app list on install/uninstall so the drawer never shows stale entries.
    private val packageReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) { apps = loadApps(this@LauncherActivity) }
    }

    @OptIn(ExperimentalComposeUiApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        WindowCompat.getInsetsController(window, window.decorView).systemBarsBehavior =
            WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        hideStatusBar()
        apps = loadApps(this)
        registerReceiver(packageReceiver, IntentFilter().apply {
            addAction(Intent.ACTION_PACKAGE_ADDED)
            addAction(Intent.ACTION_PACKAGE_REMOVED)
            addAction(Intent.ACTION_PACKAGE_CHANGED)
            addDataScheme("package")
        })
        val prefs = LauncherPrefs(this)
        tweaks.attach()
        link.start(this)
        setContent {
            val tokens = remember(prefs.scheme) { Tokens(schemeById(prefs.scheme)) }
            CompositionLocalProvider(LocalTokens provides tokens) {
                MaterialTheme(colorScheme = darkColorScheme(background = tokens.paper, surface = tokens.paper)) {
                    // Expose testTags as resource-ids so uiautomator / mobile-mcp can see the tree.
                    Box(Modifier.fillMaxSize().semantics { testTagsAsResourceId = true }) {
                        Launcher(apps, prefs, tweaks, tasks, link, homeTick, resumed)
                    }
                }
            }
        }
    }

    // Shizuku may have been started or stopped while we were away.
    override fun onResume() { super.onResume(); resumed = true; tweaks.refresh() }
    override fun onPause() { resumed = false; super.onPause() }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        homeTick++
    }

    // The system restores the status bar whenever focus returns (after an app or the shade),
    // so hiding it once in onCreate is not enough.
    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideStatusBar()
    }

    private fun hideStatusBar() =
        WindowCompat.getInsetsController(window, window.decorView).hide(WindowInsetsCompat.Type.statusBars())

    override fun onDestroy() {
        tweaks.detach()
        link.stop()
        unregisterReceiver(packageReceiver)
        super.onDestroy()
    }
}

/** One page is on screen at a time; back always steps toward Home. */
enum class Page { Home, Apps, Hub, Live }

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun Launcher(apps: List<AppEntry>, prefs: LauncherPrefs, tweaks: SystemTweaks, tasks: Tasks, link: DesktopLink, homeTick: Int, resumed: Boolean) {
    val context = LocalContext.current
    var page by remember { mutableStateOf(Page.Home) }
    var menuFor by remember { mutableStateOf<AppEntry?>(null) }
    var liveWorkspace by remember { mutableIntStateOf(0) }
    LaunchedEffect(homeTick) { page = Page.Home; menuFor = null }
    BackHandler(page != Page.Home) { page = Page.Home }
    // Declared after the page handler so it wins: back closes the menu before leaving the page.
    BackHandler(menuFor != null) { menuFor = null }

    val blur by animateDpAsState(if (page == Page.Home) 0.dp else 24.dp, tween(220), label = "blur")
    val open: (AppEntry) -> Unit = { app ->
        prefs.recordLaunch(app.packageName)
        launch(context, app)
    }

    LiveWallpaper(prefs.wallpaper, T, Modifier.fillMaxSize().blur(blur), animated = resumed && page == Page.Home)

    AnimatedVisibility(page == Page.Home, enter = fadeIn(), exit = fadeOut()) {
        Column(
            Modifier.fillMaxSize()
                .combinedClickable(interactionSource = remember { MutableInteractionSource() }, indication = null,
                    onClick = {}, onLongClick = { page = Page.Hub },
                    onDoubleClick = if (tweaks.state == SystemTweaks.State.Ready) ({ tweaks.sleep(); Unit }) else null)
                .pointerInput(Unit) {
                    detectVerticalDragGestures { _, dy ->
                        if (dy < -25) page = Page.Apps
                        else if (dy > 25) expandShade(context)
                    }
                }
                .navigationBarsPadding()
        ) {
            Bar(onSeal = { page = Page.Hub })
            // Content anchors to the top, always: widgets stack under the bar in a fixed order.
            Column(Modifier.padding(horizontal = Geo.s5)) {
                Spacer(Modifier.height(Geo.s6))
                ClockWidget(prefs.face)
                if (prefs.calendar) {
                    Spacer(Modifier.height(Geo.s5))
                    Planner(tasks, link, onView = { liveWorkspace = it; page = Page.Live })
                }
            }
            Spacer(Modifier.weight(1f))
            Dock(apps.filter { prefs.isPinned(it.packageName) }.sortedBy { prefs.pins.indexOf(it.packageName) },
                prefs.inkIcons, onOpen = open, onMenu = { menuFor = it })
            Text("APPS", color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fTiny, letterSpacing = Type.trackMark,
                modifier = Modifier.align(Alignment.CenterHorizontally).clickable { page = Page.Apps }
                    .padding(Geo.s3).testTag("open-drawer"))
        }
    }

    AnimatedVisibility(page == Page.Apps, enter = slideInVertically { it / 4 } + fadeIn(), exit = slideOutVertically { it / 4 } + fadeOut()) {
        AppsPage(apps, prefs, onOpen = { open(it); page = Page.Home }, onMenu = { menuFor = it }, onClose = { page = Page.Home })
    }

    AnimatedVisibility(page == Page.Hub, enter = fadeIn(), exit = fadeOut()) {
        HubPage(prefs, tweaks, onClose = { page = Page.Home }, onDefaultLauncher = {
            context.startActivity(Intent(Settings.ACTION_HOME_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        })
    }

    AnimatedVisibility(page == Page.Live, enter = fadeIn(), exit = fadeOut()) {
        LiveView(link, liveWorkspace, onClose = { page = Page.Home })
    }

    menuFor?.let { app ->
        // The oldest pin is the one a full dock drops, so the menu names it before it happens.
        val oldest = apps.firstOrNull { it.packageName == prefs.pins.firstOrNull() }?.label ?: ""
        AppMenu(app, prefs, tweaks, oldest, onDismiss = { menuFor = null })
    }
}

/**
 * Long-press menu: every action on an app in one place, said in words, instead of a silent
 * gesture. An overlay separates with paperLift and a lineStrong border, never a shadow.
 */
@Composable
fun AppMenu(app: AppEntry, prefs: LauncherPrefs, tweaks: SystemTweaks, oldestPin: String, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val pinned = prefs.isPinned(app.packageName)
    val dockFull = !pinned && prefs.pins.size >= LauncherPrefs.MAX_PINS
    Box(Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.55f))
        .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null, onClick = onDismiss)
        .testTag("app-menu-scrim"), contentAlignment = Alignment.BottomCenter) {
        Column(Modifier.navigationBarsPadding().padding(Geo.s4).fillMaxWidth().clip(RoundedCornerShape(Geo.radius))
            .background(T.paperLift).border(Geo.border, T.lineStrong, RoundedCornerShape(Geo.radius))
            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null, onClick = {})
            .padding(vertical = Geo.s2).testTag("app-menu")) {
            Row(Modifier.padding(horizontal = Geo.s4, vertical = Geo.s3), verticalAlignment = Alignment.CenterVertically) {
                AppGlyph(app, prefs.inkIcons, 44.dp)
                Spacer(Modifier.width(Geo.s3))
                Column {
                    Text(app.label, color = T.ink, fontFamily = Type.display, fontSize = Type.fValue, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Eyebrow(app.category.label, T.inkFaint)
                }
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(T.lineSoft))
            MenuRow(if (pinned) "Remove from dock" else "Pin to dock",
                when { pinned -> "It leaves the bottom of the home screen"
                       dockFull -> "The dock is full: replaces $oldestPin"
                       else -> "Shows at the bottom of the home screen" }, "menu:pin") {
                prefs.togglePin(app.packageName); onDismiss()
            }
            MenuRow("App info", "Permissions, storage, notifications", "menu:info") {
                context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    android.net.Uri.parse("package:${app.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                onDismiss()
            }
            if (tweaks.state == SystemTweaks.State.Ready) {
                MenuRow("Force stop", "Close it completely, background included", "menu:stop") {
                    tweaks.forceStop(app.packageName); onDismiss()
                }
                if (tweaks.canFreeze(app.packageName)) MenuRow("Freeze", "Disable it without uninstalling; undo in the Hub", "menu:freeze") {
                    tweaks.freeze(app.packageName); onDismiss()
                }
            }
            if (app.removable) MenuRow("Uninstall", "Android asks you to confirm", "menu:uninstall") {
                context.startActivity(Intent(Intent.ACTION_DELETE, android.net.Uri.parse("package:${app.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                onDismiss()
            }
        }
    }
}

@Composable
private fun MenuRow(title: String, desc: String, tag: String, onClick: () -> Unit) {
    Column(Modifier.fillMaxWidth().heightIn(min = Geo.rowH + 8.dp).clickable(onClick = onClick)
        .padding(horizontal = Geo.s4, vertical = Geo.s2).testTag(tag), verticalArrangement = Arrangement.Center) {
        Text(title, color = T.ink, fontFamily = Type.ui, fontSize = Type.fRow)
        Text(desc, color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fSmall)
    }
}

// ---- The frame: the bar carries the accent and the seal; the content never does. ----

@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
fun Bar(onSeal: () -> Unit) {
    val context = LocalContext.current
    val view = androidx.compose.ui.platform.LocalView.current
    val density = androidx.compose.ui.platform.LocalDensity.current
    val now = rememberCalendar()
    val (level, charging) = rememberBattery()
    val lineSoft = T.lineSoft
    // The bar lives inside the status-bar band and splits around the front camera, so the
    // strip beside the cutout carries the tray instead of sitting empty.
    val bandPx = WindowInsets.statusBarsIgnoringVisibility.getTop(density)
    val cutout = if (android.os.Build.VERSION.SDK_INT >= 29)
        view.rootWindowInsets?.displayCutout?.boundingRectTop?.takeUnless { it.isEmpty } else null
    val gap = with(density) { Geo.s4.roundToPx() }
    // The reported cutout is a coarse rectangle from the top edge; the lens itself sits lower in it,
    // so the bar's content line is dropped to meet the lens centre rather than the rectangle's.
    val drop = 7.5.dp
    val lineStrong = T.lineStrong
    val sun = T.sun
    BoxWithConstraints(
        Modifier.fillMaxWidth().background(T.paper.copy(alpha = 0.78f))
            .drawBehind {
                drawLine(lineSoft, Offset(0f, size.height), Offset(size.width, size.height), 1f)
                // A registration mark around the front camera, so the hole reads as part of the
                // print: a hairline ring, four ticks, and a sun-dot on the ring at twelve o'clock.
                cutout?.let { c ->
                    val center = Offset(c.exactCenterX(), (size.height + drop.toPx()) / 2)
                    val r = c.width() / 2f + 2.dp.toPx()
                    drawCircle(lineStrong, r, center, style = androidx.compose.ui.graphics.drawscope.Stroke(1f))
                    val t0 = r + 3.dp.toPx(); val t1 = r + 9.dp.toPx()
                    drawLine(lineStrong, Offset(center.x - t1, center.y), Offset(center.x - t0, center.y), 1f)
                    drawLine(lineStrong, Offset(center.x + t0, center.y), Offset(center.x + t1, center.y), 1f)
                    drawLine(lineStrong, Offset(center.x, center.y + t0), Offset(center.x, size.height), 1f)
                    drawCircle(sun, 1.6.dp.toPx(), Offset(center.x + r * 0.707f, center.y - r * 0.707f))
                }
            }
            .height(with(density) { maxOf(bandPx, cutout?.bottom ?: 0).toDp() }.coerceAtLeast(32.dp) + drop)
            .padding(horizontal = Geo.s2).testTag("topbar"),
    ) {
        val widthPx = with(density) { maxWidth.roundToPx() } + with(density) { (Geo.s2 * 2).roundToPx() }
        val pad = with(density) { Geo.s2.roundToPx() }
        // Without a cutout the halves meet in the middle; with one they stop a gap short of it.
        val leftPx = (cutout?.left ?: (widthPx / 2)) - pad - gap
        val rightPx = widthPx - (cutout?.right ?: (widthPx / 2)) - pad - gap
        Row(Modifier.align(Alignment.CenterStart).width(with(density) { leftPx.toDp() }).fillMaxHeight().padding(top = drop),
            verticalAlignment = Alignment.CenterVertically) {
            Text("力", color = Tokens.brand, fontFamily = Type.jp, fontSize = Type.fRow, fontWeight = FontWeight.Bold,
                modifier = Modifier.clip(RoundedCornerShape(Geo.radius)).clickable(onClick = onSeal).padding(horizontal = Geo.s1 + 2.dp).testTag("hub"))
            Text("%02d:%02d".format(now.get(java.util.Calendar.HOUR_OF_DAY), now.get(java.util.Calendar.MINUTE)),
                color = T.ink, fontFamily = Type.mono, fontSize = Type.fSmall, maxLines = 1)
            Spacer(Modifier.weight(1f))
            // Music: the note opens the player.
            BarGlyph("♪", "music") {
                if (!Music.open(context)) Toast.makeText(context, "No music player installed", Toast.LENGTH_SHORT).show()
            }
        }
        Row(Modifier.align(Alignment.CenterEnd).width(with(density) { rightPx.toDp() }).fillMaxHeight().padding(top = drop),
            verticalAlignment = Alignment.CenterVertically) {
            Spacer(Modifier.weight(1f))
            NetGlyph(rememberNet())
            Spacer(Modifier.width(Geo.s2))
            BatteryGlyph(level, charging)
            Spacer(Modifier.width(Geo.s1))
            Text("$level", color = if (level <= 15) Tokens.alert else T.inkDim, fontFamily = Type.mono, fontSize = Type.fSmall, maxLines = 1)
        }
    }
}

@Composable
private fun BarGlyph(glyph: String, tag: String, onClick: () -> Unit) {
    Box(Modifier.fillMaxHeight().width(30.dp).clip(RoundedCornerShape(Geo.radius)).clickable(onClick = onClick).testTag(tag), contentAlignment = Alignment.Center) {
        Text(glyph, color = T.ink, fontSize = Type.fBody)
    }
}

// ---- Shared idioms ----

/** A surface lifted off the sheet: paperLift, one hairline, a hair of rounding, no shadow. */
@Composable
fun Card(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.fillMaxWidth().clip(RoundedCornerShape(Geo.radius)).background(T.paperLift.copy(alpha = 0.92f))
        .border(Geo.border, T.line, RoundedCornerShape(Geo.radius)).padding(Geo.s4), content = content)
}

/** Mono, uppercase, wide tracking: the technical poster label. */
@Composable
fun Eyebrow(text: String, color: Color = T.inkMuted, modifier: Modifier = Modifier) {
    Text(text.uppercase(), color = color, fontFamily = Type.mono, fontSize = Type.fMicro, letterSpacing = Type.trackLabel, modifier = modifier)
}

/** Section head: the Latin word names it, the kanji gloss seals it. */
@Composable
fun SectionHead(latin: String, gloss: String) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = Geo.s5, bottom = Geo.s2)) {
        if (gloss.isNotEmpty()) {
            Text(gloss, color = T.inkDim, fontFamily = Type.jp, fontSize = Type.fRow)
            Spacer(Modifier.width(Geo.s2))
        }
        Eyebrow(latin)
    }
}

/** A choice among a few modes: segments where the selected one flips to a bone plate. */
@Composable
fun <V> Seg(options: List<V>, selected: V, label: (V) -> String, onPick: (V) -> Unit) {
    Row(Modifier.fillMaxWidth().height(Geo.ctlH + 14.dp).clip(RoundedCornerShape(Geo.radius))
        .border(Geo.border, T.line, RoundedCornerShape(Geo.radius))) {
        options.forEach { o ->
            val on = o == selected
            Box(Modifier.weight(1f).fillMaxHeight().background(if (on) T.bone else Color.Transparent).clickable { onPick(o) }
                .testTag("seg:${label(o)}"), contentAlignment = Alignment.Center) {
                Text(label(o), color = if (on) T.inkOnBone else T.inkDim, fontFamily = Type.ui, fontSize = Type.fSmall)
            }
        }
    }
}

/** A true binary, as a full settings row. The track tints and the knob fills as it goes on. */
@Composable
fun SwRow(title: String, desc: String, on: Boolean, onToggle: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = Geo.rowH).clickable { onToggle(!on) }.testTag("sw:$title"),
        verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, color = T.ink, fontFamily = Type.ui, fontSize = Type.fRow)
            Text(desc, color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fSmall)
        }
        Box(Modifier.width(40.dp).height(22.dp).clip(CircleShape).background(if (on) T.bone else Color.Transparent)
            .border(Geo.border, T.lineStrong, CircleShape).padding(3.dp),
            contentAlignment = if (on) Alignment.CenterEnd else Alignment.CenterStart) {
            Box(Modifier.size(16.dp).clip(CircleShape).background(if (on) T.inkOnBone else T.inkMuted))
        }
    }
}

@Composable
fun Btn(text: String, tag: String, onClick: () -> Unit) {
    Box(Modifier.clip(RoundedCornerShape(Geo.radius)).border(Geo.border, T.lineStrong, RoundedCornerShape(Geo.radius))
        .clickable(onClick = onClick).padding(horizontal = Geo.s4, vertical = Geo.s2).testTag(tag)) {
        Text(text, color = T.ink, fontFamily = Type.ui, fontSize = Type.fBody)
    }
}

/** Page head: eyebrow, Fraunces title, and the way back. */
@Composable
fun PageHead(eyebrow: String, title: String, onClose: () -> Unit) {
    Row(Modifier.fillMaxWidth().pointerInput(Unit) { detectVerticalDragGestures { _, dy -> if (dy > 25) onClose() } },
        verticalAlignment = Alignment.Bottom) {
        Column(Modifier.weight(1f)) {
            Eyebrow(eyebrow, T.inkFaint)
            Text(title, color = T.ink, fontFamily = Type.display, fontSize = Type.fTitle)
        }
        Btn("← Home", "close", onClose)
    }
}

// ---- Icons: one ink for every app, so the grid reads as one printed sheet. ----

private val desaturate = ColorFilter.colorMatrix(ColorMatrix().apply { setToSaturation(0f) })

@Composable
fun AppGlyph(app: AppEntry, ink: Boolean, size: Dp) {
    if (!ink) { Image(app.icon, app.label, Modifier.size(size)); return }
    // A plate per app: paperLift with a hairline; the glyph is the app's own monochrome layer
    // tinted in ink, or its full icon desaturated when the app ships no monochrome layer.
    Box(Modifier.size(size).clip(RoundedCornerShape(Geo.radius + 4.dp)).background(T.paperLift)
        .border(Geo.border, T.line, RoundedCornerShape(Geo.radius + 4.dp)), contentAlignment = Alignment.Center) {
        if (app.mono != null) Image(app.mono, app.label, Modifier.size(size * 0.9f), colorFilter = ColorFilter.tint(T.ink))
        else Image(app.icon, app.label, Modifier.size(size * 0.72f).clip(CircleShape), colorFilter = desaturate, alpha = 0.85f)
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun AppCell(app: AppEntry, prefs: LauncherPrefs, onMenu: (AppEntry) -> Unit, onOpen: () -> Unit) {
    val pinned = prefs.isPinned(app.packageName)
    Column(
        Modifier.clip(RoundedCornerShape(Geo.radius))
            .combinedClickable(onClick = onOpen, onLongClick = { onMenu(app) })
            .padding(vertical = Geo.s1).testTag("app:${app.packageName}"),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box {
            AppGlyph(app, prefs.inkIcons, 52.dp)
            // Pinned is data, so it may carry a mark: a small bone dot, not a colour.
            if (pinned) Box(Modifier.size(8.dp).align(Alignment.TopEnd).clip(CircleShape).background(T.bone))
        }
        if (prefs.labels) {
            Spacer(Modifier.height(Geo.s1 + 2.dp))
            Text(app.label, color = T.inkDim, fontFamily = Type.ui, fontSize = Type.fMicro, maxLines = 1,
                overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(horizontal = 2.dp))
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun Dock(pinned: List<AppEntry>, ink: Boolean, onOpen: (AppEntry) -> Unit, onMenu: (AppEntry) -> Unit) {
    // A dock island genuinely floats, so it is the one content surface with a stronger edge.
    Row(
        Modifier.padding(horizontal = Geo.s4).fillMaxWidth().clip(RoundedCornerShape(Geo.radius + 4.dp))
            .background(T.paperLift.copy(alpha = 0.9f)).border(Geo.border, T.lineStrong, RoundedCornerShape(Geo.radius + 4.dp))
            .padding(Geo.s3).heightIn(min = 52.dp).testTag("dock"),
        horizontalArrangement = Arrangement.SpaceEvenly,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (pinned.isEmpty()) Eyebrow("Long-press an app to pin it here", T.inkFaint)
        pinned.forEach { app ->
            Box(Modifier.clip(RoundedCornerShape(Geo.radius)).combinedClickable(onClick = { onOpen(app) }, onLongClick = { onMenu(app) })
                .testTag("dock:${app.packageName}")) { AppGlyph(app, ink, 48.dp) }
        }
    }
}

// ---- Apps page ----

@Composable
fun AppsPage(apps: List<AppEntry>, prefs: LauncherPrefs, onOpen: (AppEntry) -> Unit, onMenu: (AppEntry) -> Unit, onClose: () -> Unit) {
    var query by remember { mutableStateOf("") }
    var filter by remember { mutableStateOf<Category?>(null) }
    val present = remember(apps) { Category.entries.filter { c -> apps.any { it.category == c } } }
    val shown = remember(apps, query, filter) {
        apps.filter { (filter == null || it.category == filter) && it.label.contains(query, ignoreCase = true) }
    }
    val frequent = prefs.frequent(prefs.columns).mapNotNull { pkg -> apps.firstOrNull { it.packageName == pkg } }
    // Letter heads only pay off on long lists; on short ones they leave mostly-empty rows.
    val sections = remember(shown, filter, query) {
        if (filter == null && query.isEmpty() && shown.size > 40)
            shown.groupBy { it.label.first().uppercaseChar().takeIf(Char::isLetter) ?: '#' }.toSortedMap()
        else sortedMapOf(' ' to shown)
    }
    val full: LazyGridItemSpanScope.() -> GridItemSpan = { GridItemSpan(maxLineSpan) }

    Column(Modifier.fillMaxSize().background(T.paper.copy(alpha = 0.82f)).topSafe().navigationBarsPadding()
        .imePadding().padding(horizontal = Geo.s4).testTag("drawer")) {
        Spacer(Modifier.height(Geo.s4))
        PageHead("${apps.size} apps", "Apps", onClose)
        Spacer(Modifier.height(Geo.s4))
        SearchField(query) { query = it }
        Spacer(Modifier.height(Geo.s3))
        // Tabs: bone-invert plates with the // lead.
        LazyRow(horizontalArrangement = Arrangement.spacedBy(Geo.s1)) {
            item { Tab("All", "", filter == null) { filter = null } }
            items(present) { c -> Tab(c.label, "", filter == c) { filter = if (filter == c) null else c } }
        }
        LazyVerticalGrid(GridCells.Fixed(prefs.columns), verticalArrangement = Arrangement.spacedBy(Geo.s3),
            contentPadding = PaddingValues(vertical = Geo.s4)) {
            if (frequent.isNotEmpty() && filter == null && query.isEmpty()) {
                item(span = full) { Eyebrow("Frequent", T.inkFaint, Modifier.padding(bottom = Geo.s1)) }
                items(frequent, key = { "f:" + it.component.flattenToString() }) { app -> AppCell(app, prefs, onMenu) { onOpen(app) } }
                item(span = full) { Box(Modifier.fillMaxWidth().padding(vertical = Geo.s2).height(1.dp).background(T.lineSoft)) }
            }
            sections.forEach { (letter, list) ->
                if (letter != ' ') item(span = full, key = "h:$letter") { Eyebrow(letter.toString(), T.inkFaint) }
                items(list, key = { it.component.flattenToString() }) { app -> AppCell(app, prefs, onMenu) { onOpen(app); query = "" } }
            }
            if (shown.isEmpty()) item(span = full) {
                Text("Nothing matches “$query”.", color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fBody,
                    modifier = Modifier.padding(top = Geo.s5))
            }
        }
    }
}

@Composable
fun Tab(latin: String, gloss: String, on: Boolean, onClick: () -> Unit) {
    Row(Modifier.clip(RoundedCornerShape(Geo.radius)).background(if (on) T.bone else Color.Transparent)
        .border(Geo.border, if (on) T.bone else T.line, RoundedCornerShape(Geo.radius))
        .clickable(onClick = onClick).padding(horizontal = Geo.s3, vertical = Geo.s2).testTag("tab:$latin"),
        verticalAlignment = Alignment.CenterVertically) {
        val ink = if (on) T.inkOnBone else T.inkDim
        if (on) Text("// ", color = ink, fontFamily = Type.mono, fontSize = Type.fMicro)
        Text(latin.uppercase(), color = ink, fontFamily = Type.mono, fontSize = Type.fMicro, letterSpacing = Type.trackLabel)
        if (gloss.isNotEmpty()) {
            Spacer(Modifier.width(Geo.s1 + 2.dp))
            Text(gloss, color = ink, fontFamily = Type.jp, fontSize = Type.fMicro)
        }
    }
}

@Composable
fun SearchField(value: String, onChange: (String) -> Unit) {
    Row(Modifier.fillMaxWidth().height(Geo.rowH).clip(RoundedCornerShape(Geo.radius)).background(T.paperLift)
        .border(Geo.border, T.line, RoundedCornerShape(Geo.radius)).padding(horizontal = Geo.s4),
        verticalAlignment = Alignment.CenterVertically) {
        Text("⌕", color = T.inkFaint, fontSize = Type.fRow)
        Spacer(Modifier.width(Geo.s3))
        Box(Modifier.weight(1f)) {
            if (value.isEmpty()) Text("Search apps", color = T.inkFaint, fontFamily = Type.ui, fontSize = Type.fRow)
            BasicTextField(value, onChange, singleLine = true, cursorBrush = SolidColor(T.ink),
                textStyle = TextStyle(color = T.ink, fontFamily = Type.ui, fontSize = Type.fRow),
                modifier = Modifier.fillMaxWidth().testTag("search"))
        }
    }
}

// ---- Hub page: the phone's small Ryoku Hub. Leads with what a user came for: the look. ----

@Composable
fun HubPage(prefs: LauncherPrefs, tweaks: SystemTweaks, onClose: () -> Unit, onDefaultLauncher: () -> Unit) {
    val context = LocalContext.current
    Column(Modifier.fillMaxSize().background(T.paper).topSafe().navigationBarsPadding()
        .verticalScroll(rememberScrollState()).padding(horizontal = Geo.s4).padding(bottom = Geo.s6).testTag("hub-sheet")) {
        Spacer(Modifier.height(Geo.s4))
        PageHead("力  ·  Settings", "Ryoku Hub", onClose)

        SectionHead("Colour scheme", "")
        LazyRow(horizontalArrangement = Arrangement.spacedBy(Geo.s2)) {
            items(schemes) { s ->
                SchemeSwatch(s, s.id == prefs.scheme) { prefs.chooseScheme(s.id) }
            }
        }

        SectionHead("Wallpaper", "")
        LazyRow(horizontalArrangement = Arrangement.spacedBy(Geo.s2)) {
            items(Wallpaper.entries) { w ->
                val on = w == prefs.wallpaper
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    LiveWallpaper(w, T, animated = false, modifier = Modifier.width(76.dp).height(132.dp)
                        .clip(RoundedCornerShape(Geo.radius))
                        .border(if (on) 2.dp else Geo.border, if (on) T.bone else T.line, RoundedCornerShape(Geo.radius))
                        .clickable { prefs.chooseWallpaper(w) }.testTag("wp:${w.name}"))
                    Spacer(Modifier.height(Geo.s1))
                    Text(w.label, color = if (on) T.ink else T.inkMuted, fontFamily = Type.ui, fontSize = Type.fMicro)
                }
            }
        }

        SectionHead("Clock", "")
        Seg(Face.entries, prefs.face, { it.label }, prefs::chooseFace)
        SwRow("Calendar & tasks", "The month and its tasks under the clock", prefs.calendar, prefs::chooseCalendar)

        SectionHead("App launcher", "")
        Seg(listOf(4, 5), prefs.columns, { "$it columns" }, prefs::chooseColumns)
        SwRow("Ink icons", "Every icon in one ink", prefs.inkIcons, prefs::chooseInkIcons)
        SwRow("App names", "A label under each icon", prefs.labels, prefs::chooseLabels)

        SectionHead("System", "")
        ShizukuPanel(tweaks)
        Spacer(Modifier.height(Geo.s3))
        Btn("Set as default launcher", "default-launcher", onDefaultLauncher)
    }
}

/** A palette is data, so its swatch shows its own colours: a paper chip with ink, accent and tones. */
@Composable
fun SchemeSwatch(s: Scheme, on: Boolean, onClick: () -> Unit) {
    val c = s.swatches.map { Color(it) }
    Column(Modifier.width(96.dp).clickable(onClick = onClick).testTag("scheme:${s.id}")) {
        Column(Modifier.fillMaxWidth().height(64.dp).clip(RoundedCornerShape(Geo.radius)).background(c[0])
            .border(if (on) 2.dp else Geo.border, if (on) T.bone else T.line, RoundedCornerShape(Geo.radius)).padding(Geo.s2)) {
            Text("Aa", color = c[1], fontFamily = Type.display, fontSize = Type.fRow)
            Spacer(Modifier.weight(1f))
            Row(horizontalArrangement = Arrangement.spacedBy(3.dp)) {
                listOf(c[2], c[3], c[4], c[5]).forEach { Box(Modifier.size(10.dp).clip(CircleShape).background(it)) }
            }
        }
        Spacer(Modifier.height(Geo.s1))
        Text(s.label, color = if (on) T.ink else T.inkMuted, fontFamily = Type.ui, fontSize = Type.fMicro, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

/** Battery level and charging state from the sticky ACTION_BATTERY_CHANGED broadcast. */
@Composable
fun rememberBattery(): Pair<Int, Boolean> {
    val context = LocalContext.current
    var state by remember { mutableStateOf(0 to false) }
    DisposableEffect(Unit) {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, 0) * 100 / i.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
                val status = i.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
                state = level to (status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL)
            }
        }
        context.registerReceiver(receiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        onDispose { context.unregisterReceiver(receiver) }
    }
    return state
}

/**
 * Top inset that survives a hidden status bar. Once the bar is hidden its inset drops to zero,
 * which would slide our bar under the camera cutout; the "ignoring visibility" height plus the
 * cutout keeps the space reserved either way.
 */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
fun Modifier.topSafe() = composed { windowInsetsPadding(
    WindowInsets.statusBarsIgnoringVisibility.union(WindowInsets.displayCutout).only(WindowInsetsSides.Top)) }

/**
 * Shell-level extras through Shizuku. A control that cannot work is never offered: until Shizuku
 * runs and has granted access, the panel shows only the one step that gets it there.
 */
@Composable
fun ShizukuPanel(tweaks: SystemTweaks) {
    val context = LocalContext.current
    val ready = tweaks.state == SystemTweaks.State.Ready
    val (status, action) = when (tweaks.state) {
        SystemTweaks.State.NotInstalled -> "Shizuku is not installed" to "Get Shizuku"
        SystemTweaks.State.NotRunning -> "Shizuku is installed but not running" to "Open Shizuku"
        SystemTweaks.State.NoPermission -> "Shizuku is running; allow Ryoku to use it" to "Allow access"
        SystemTweaks.State.Ready -> "Shizuku ready: shell powers without root" to null
    }
    Eyebrow("Shizuku  ·  ${tweaks.state.name}", if (ready) T.inkDim else T.inkFaint)
    Text(status, color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fSmall, modifier = Modifier.padding(vertical = Geo.s1))
    if (action != null) {
        Spacer(Modifier.height(Geo.s2))
        Btn(action, "shizuku-action") {
            when (tweaks.state) {
                SystemTweaks.State.NotInstalled -> context.startActivity(Intent(Intent.ACTION_VIEW,
                    android.net.Uri.parse("https://shizuku.rikka.app/download/")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                SystemTweaks.State.NotRunning -> context.packageManager.getLaunchIntentForPackage(SystemTweaks.SHIZUKU_PACKAGE)
                    ?.let { context.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                SystemTweaks.State.NoPermission -> tweaks.requestPermission()
                SystemTweaks.State.Ready -> Unit
            }
        }
    }
    // The grants persist after a reboot, so once held they work even with Shizuku stopped.
    if (!tweaks.granted && ready) {
        Spacer(Modifier.height(Geo.s3))
        Text("One step unlocks system animations, screen time and now playing, and keeps them after a reboot.",
            color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fSmall)
        Spacer(Modifier.height(Geo.s2))
        Btn("Grant permissions", "grant") { tweaks.grantAll() }
    }
    if (tweaks.granted) {
        Spacer(Modifier.height(Geo.s4))
        Eyebrow("System animations", T.inkFaint)
        Spacer(Modifier.height(Geo.s2))
        var scale by remember { mutableStateOf(tweaks.animationScale) }
        Seg(listOf(1f, 0.5f, 0f), scale, { when (it) { 1f -> "Normal"; 0.5f -> "Fast"; else -> "Off" } }) {
            tweaks.setAnimationScale(it); scale = it
        }
    }
    if (ready && tweaks.frozen.isNotEmpty()) {
        Spacer(Modifier.height(Geo.s4))
        // A frozen app leaves the drawer, so this list is the only way back to it.
        Eyebrow("Frozen apps  ·  ${tweaks.frozen.size}", T.inkFaint)
        tweaks.frozen.forEach { pkg ->
            Row(Modifier.fillMaxWidth().heightIn(min = Geo.rowH).testTag("frozen:$pkg"), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(tweaks.labelOf(pkg), color = T.ink, fontFamily = Type.ui, fontSize = Type.fRow)
                    Text(pkg, color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fTiny)
                }
                Btn("Unfreeze", "unfreeze:$pkg") { tweaks.unfreeze(pkg) }
            }
        }
    }
    tweaks.lastError?.let { Text(it, color = Tokens.alert, fontFamily = Type.mono, fontSize = Type.fMicro, modifier = Modifier.padding(top = Geo.s2)) }
}

/** Opens the notification shade; EXPAND_STATUS_BAR is a normal permission, no Shizuku needed. */
fun expandShade(context: Context) {
    runCatching {
        val sb = context.getSystemService("statusbar")
        sb.javaClass.getMethod("expandNotificationsPanel").invoke(sb)
    }
}
