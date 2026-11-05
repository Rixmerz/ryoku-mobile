package com.rixmerz.ryokumobile

import android.content.Context
import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.ui.graphics.Color
import java.util.Calendar
import java.util.Locale

/** Clock faces, after the desktop's shell/modules/desktop/clock faces. */
enum class Face(val label: String, val gloss: String) {
    Grand("Grand", "大"), Kanji("Kanji", "漢"), Hour("Hour", "時"), Minimal("Minimal", "簡")
}

fun faceById(id: String) = Face.entries.firstOrNull { it.name == id } ?: Face.Grand

@Composable
fun rememberCalendar(): Calendar {
    var now by remember { mutableStateOf(Calendar.getInstance()) }
    LaunchedEffect(Unit) {
        while (true) {
            now = Calendar.getInstance()
            delay(1_000 - System.currentTimeMillis() % 1_000)
        }
    }
    return now
}

private fun pad(n: Int) = n.toString().padStart(2, '0')
private fun Calendar.weekday(style: Int) = getDisplayName(Calendar.DAY_OF_WEEK, style, Locale.ENGLISH)!!
private fun Calendar.month(style: Int) = getDisplayName(Calendar.MONTH, style, Locale.ENGLISH)!!

// Weekday initials, Monday-first (ISO 8601), the week Chile and most of the world uses.
private val weekInitials = listOf("M", "T", "W", "T", "F", "S", "S")

// Sunday-first, matching Calendar.DAY_OF_WEEK (1 = Sunday).
private val kanjiDays = listOf("日", "月", "火", "水", "木", "金", "土")

/** 0..99 in kanji numerals: 37 -> 三十七, 10 -> 十, 0 -> 零. */
private fun kanjiNumber(n: Int): String {
    val d = listOf("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")
    if (n == 0) return d[0]
    val tens = n / 10; val ones = n % 10
    return (if (tens > 1) d[tens] else "") + (if (tens > 0) "十" else "") + (if (ones > 0) d[ones] else "")
}

@Composable
fun ClockWidget(face: Face) {
    val now = rememberCalendar()
    val hh = pad(now.get(Calendar.HOUR_OF_DAY)); val mm = pad(now.get(Calendar.MINUTE)); val ss = pad(now.get(Calendar.SECOND))
    Box(Modifier.testTag("clock")) {
        when (face) {
            // One giant time in the display serif; the colon carries the accent.
            Face.Grand -> Column {
                Row {
                    val st = TextStyle(fontFamily = Type.display, fontWeight = FontWeight.Medium, fontSize = 104.sp, lineHeight = 104.sp)
                    Text(hh, style = st, color = T.ink); Text(":", style = st, color = T.sun); Text(mm, style = st, color = T.ink)
                }
                Eyebrow("${now.weekday(Calendar.SHORT)} ${pad(now.get(Calendar.DAY_OF_MONTH))} ${now.month(Calendar.SHORT)}")
            }
            // Latin names the thing, kanji seals it: the time read out in kanji numerals.
            Face.Kanji -> Row(verticalAlignment = Alignment.Top) {
                Column {
                    Text("${kanjiNumber(now.get(Calendar.HOUR_OF_DAY))}時", color = T.ink, fontFamily = Type.jp, fontSize = 64.sp, fontWeight = FontWeight.Bold, lineHeight = 70.sp)
                    Text("${kanjiNumber(now.get(Calendar.MINUTE))}分", color = T.inkDim, fontFamily = Type.jp, fontSize = 64.sp, fontWeight = FontWeight.Light, lineHeight = 70.sp)
                    Spacer(Modifier.height(Geo.s3))
                    Eyebrow("$hh:$mm  ·  ${now.weekday(Calendar.LONG)}")
                }
                Spacer(Modifier.weight(1f))
                // A vertical seal column, read top to bottom like a hanging scroll.
                Column(horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.border(Geo.border, T.line, RoundedCornerShape(Geo.radius)).padding(horizontal = Geo.s2, vertical = Geo.s3)) {
                    "${now.get(Calendar.MONTH) + 1}月${now.get(Calendar.DAY_OF_MONTH)}日".let { s ->
                        listOf("${kanjiNumber(now.get(Calendar.MONTH) + 1)}", "月", "${kanjiNumber(now.get(Calendar.DAY_OF_MONTH))}", "日",
                            kanjiDays[now.get(Calendar.DAY_OF_WEEK) - 1]).forEach {
                            Text(it, color = if (it == kanjiDays[now.get(Calendar.DAY_OF_WEEK) - 1]) Tokens.brand else T.ink,
                                fontFamily = Type.jp, fontSize = 18.sp, textAlign = TextAlign.Center)
                        }
                    }
                }
            }
            // A giant hour with the minute stacked solid over the second in hollow outline.
            Face.Hour -> Row(verticalAlignment = Alignment.CenterVertically) {
                Text(hh, color = T.ink, fontFamily = Type.ui, fontWeight = FontWeight.Medium, fontSize = 150.sp, lineHeight = 150.sp, letterSpacing = (-6).sp)
                Spacer(Modifier.width(Geo.s2))
                Column {
                    Text(mm, color = T.ink, fontFamily = Type.ui, fontWeight = FontWeight.Medium, fontSize = 56.sp, lineHeight = 58.sp)
                    Text(ss, style = TextStyle(fontFamily = Type.ui, fontWeight = FontWeight.Medium, fontSize = 56.sp, lineHeight = 58.sp,
                        color = T.inkMuted, drawStyle = Stroke(width = 2f)))
                    Text(now.month(Calendar.SHORT).uppercase(), style = TextStyle(fontFamily = Type.ui, fontSize = 22.sp,
                        letterSpacing = Type.trackMark, color = T.sun, drawStyle = Stroke(width = 1.5f)))
                }
            }
            Face.Minimal -> Column {
                Text("$hh:$mm", color = T.ink, fontFamily = Type.mono, fontSize = Type.fHero)
                Text("${now.weekday(Calendar.LONG)}, ${now.get(Calendar.DAY_OF_MONTH)} ${now.month(Calendar.LONG)}", color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fBody)
            }
        }
    }
}

/**
 * The planner: the month grid and the day's task list side by side in a pager, so a swipe on
 * the calendar turns to the tasks. Both pages share one fixed height, so the dock never jumps.
 */
@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun Planner(tasks: Tasks, link: DesktopLink, onView: (Int) -> Unit) {
    val now = rememberCalendar()
    var selected by remember { mutableStateOf(Tasks.key(now)) }
    // The desktop page exists only while the computer is connected: no dead page to swipe into.
    val pages = if (link.connected) 3 else 2
    val pager = androidx.compose.foundation.pager.rememberPagerState { pages }
    val scope = rememberCoroutineScope()
    Column {
        androidx.compose.foundation.pager.HorizontalPager(pager, pageSpacing = Geo.s3, modifier = Modifier.testTag("planner")) { page ->
            Card(Modifier.height(318.dp)) {
                when (page) {
                    0 -> CalendarGrid(now, tasks, selected) { key ->
                        selected = key
                        scope.launch { pager.animateScrollToPage(1) }
                    }
                    1 -> TaskList(tasks, selected, onBack = { scope.launch { pager.animateScrollToPage(0) } })
                    else -> DesktopPage(link, onView)
                }
            }
        }
        Spacer(Modifier.height(Geo.s2))
        Row(Modifier.align(Alignment.CenterHorizontally), horizontalArrangement = Arrangement.spacedBy(Geo.s2)) {
            repeat(pages) { i ->
                Box(Modifier.size(width = if (pager.currentPage == i) 14.dp else 5.dp, height = 5.dp).clip(CircleShape)
                    .background(if (pager.currentPage == i) T.inkDim else T.line))
            }
        }
    }
}

/** Month grid, Monday-first: today is a bone plate (emphasis is inversion), Sundays one tier fainter. */
@Composable
private fun CalendarGrid(now: Calendar, tasks: Tasks, selected: String, onPick: (String) -> Unit) {
    val today = now.get(Calendar.DAY_OF_MONTH)
    val first = (now.clone() as Calendar).apply { set(Calendar.DAY_OF_MONTH, 1) }
    // DAY_OF_WEEK is Sunday = 1; shift so Monday opens the week.
    val lead = (first.get(Calendar.DAY_OF_WEEK) + 5) % 7
    val days = now.getActualMaximum(Calendar.DAY_OF_MONTH)
    val prefix = Tasks.key(now).dropLast(2)
    Row(verticalAlignment = Alignment.Bottom, modifier = Modifier.testTag("calendar")) {
        Text(now.month(Calendar.LONG), color = T.ink, fontFamily = Type.display, fontSize = Type.fValue)
        Spacer(Modifier.width(Geo.s2))
        Text("${now.get(Calendar.YEAR)}", color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fSmall, modifier = Modifier.padding(bottom = 4.dp))
        Spacer(Modifier.weight(1f))
        Eyebrow("Tasks ›", T.inkFaint)
    }
    Spacer(Modifier.height(Geo.s3))
    Row {
        weekInitials.forEach { Text(it, color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fMicro, textAlign = TextAlign.Center, modifier = Modifier.weight(1f)) }
    }
    Spacer(Modifier.height(Geo.s1))
    val cells = List(lead) { 0 } + (1..days).toList()
    cells.chunked(7).forEach { week ->
        Row(Modifier.padding(vertical = 1.dp)) {
            (week + List(7 - week.size) { 0 }).forEachIndexed { i, d ->
                val key = prefix + pad(d)
                val isToday = d == today
                val isSelected = key == selected && !isToday
                val (total, done) = if (d > 0) tasks.countFor(key) else 0 to 0
                Box(Modifier.weight(1f).height(32.dp).padding(horizontal = 2.dp).clip(RoundedCornerShape(Geo.radius))
                    .background(if (isToday) T.bone else Color.Transparent)
                    .border(Geo.border, if (isSelected) T.lineStrong else Color.Transparent, RoundedCornerShape(Geo.radius))
                    .then(if (d > 0) Modifier.clickable { onPick(key) }.testTag("day:$key") else Modifier),
                    contentAlignment = Alignment.Center) {
                    if (d > 0) Text("$d", fontFamily = Type.ui, fontSize = Type.fSmall,
                        color = when { isToday -> T.inkOnBone; i == 6 -> T.inkFaint; else -> T.inkDim })
                    // A day with tasks carries a dot: the accent while any are open, faint once all are done.
                    if (total > 0) Box(Modifier.align(Alignment.BottomCenter).padding(bottom = 3.dp).size(4.dp).clip(CircleShape)
                        .background(if (done < total) T.sun else if (isToday) T.inkOnBone else T.inkFaint))
                }
            }
        }
    }
}

/** One day's tasks: type a line to add ("09:30 Call the bank" sets a time), tap the box to tick. */
@Composable
private fun TaskList(tasks: Tasks, date: String, onBack: () -> Unit) {
    var draft by remember { mutableStateOf("") }
    val list = tasks.forDate(date)
    val cal = remember(date) { Calendar.getInstance().apply { time = java.text.SimpleDateFormat("yyyy-MM-dd", Locale.ENGLISH).parse(date)!! } }
    Row(verticalAlignment = Alignment.Bottom, modifier = Modifier.testTag("tasks")) {
        Text("Tasks", color = T.ink, fontFamily = Type.display, fontSize = Type.fValue)
        Spacer(Modifier.width(Geo.s2))
        Text("${cal.weekday(Calendar.SHORT)} ${pad(cal.get(Calendar.DAY_OF_MONTH))} ${cal.month(Calendar.SHORT)}".uppercase(),
            color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fSmall, modifier = Modifier.padding(bottom = 4.dp))
        Spacer(Modifier.weight(1f))
        Eyebrow("‹ Month", T.inkFaint, Modifier.clickable(onClick = onBack).testTag("tasks-back"))
    }
    Spacer(Modifier.height(Geo.s3))
    Row(Modifier.fillMaxWidth().height(40.dp).clip(RoundedCornerShape(Geo.radius)).border(Geo.border, T.line, RoundedCornerShape(Geo.radius))
        .padding(horizontal = Geo.s3), verticalAlignment = Alignment.CenterVertically) {
        Text("+", color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fRow)
        Spacer(Modifier.width(Geo.s2))
        Box(Modifier.weight(1f)) {
            if (draft.isEmpty()) Text("Add a task  ·  09:30 Call…", color = T.inkFaint, fontFamily = Type.ui, fontSize = Type.fBody)
            androidx.compose.foundation.text.BasicTextField(draft, { draft = it }, singleLine = true,
                cursorBrush = androidx.compose.ui.graphics.SolidColor(T.ink),
                textStyle = TextStyle(color = T.ink, fontFamily = Type.ui, fontSize = Type.fBody),
                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(imeAction = androidx.compose.ui.text.input.ImeAction.Done),
                keyboardActions = androidx.compose.foundation.text.KeyboardActions(onDone = { if (tasks.add(date, draft)) draft = "" }),
                modifier = Modifier.fillMaxWidth().testTag("task-input"))
        }
    }
    Spacer(Modifier.height(Geo.s2))
    if (list.isEmpty()) {
        Text("Nothing planned for this day.", color = T.inkMuted, fontFamily = Type.ui, fontSize = Type.fSmall, modifier = Modifier.padding(top = Geo.s2))
    }
    androidx.compose.foundation.lazy.LazyColumn {
        items(list.size, key = { list[it].id }) { idx ->
            val t = list[idx]
            Row(Modifier.fillMaxWidth().heightIn(min = 40.dp).testTag("task:${t.id}"), verticalAlignment = Alignment.CenterVertically) {
                // The tick is an inversion: an empty hairline box that fills to a bone plate.
                Box(Modifier.size(20.dp).clip(RoundedCornerShape(4.dp)).background(if (t.done) T.bone else Color.Transparent)
                    .border(Geo.border, T.lineStrong, RoundedCornerShape(4.dp)).clickable { tasks.toggle(t.id) }.testTag("tick:${t.id}"),
                    contentAlignment = Alignment.Center) {
                    if (t.done) Text("✓", color = T.inkOnBone, fontSize = Type.fMicro)
                }
                Spacer(Modifier.width(Geo.s3))
                if (t.time.isNotEmpty()) {
                    Text(t.time, color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fSmall)
                    Spacer(Modifier.width(Geo.s2))
                }
                Text(t.text, color = if (t.done) T.inkFaint else T.ink, fontFamily = Type.ui, fontSize = Type.fBody,
                    textDecoration = if (t.done) androidx.compose.ui.text.style.TextDecoration.LineThrough else null,
                    maxLines = 2, modifier = Modifier.weight(1f))
                Text("×", color = T.inkFaint, fontSize = Type.fRow,
                    modifier = Modifier.clip(CircleShape).clickable { tasks.remove(t.id) }.padding(horizontal = Geo.s2).testTag("remove:${t.id}"))
            }
        }
    }
}

/** Music shortcut for the bar: opens the first installed player. */
object Music {
    private val players = listOf(
        "com.google.android.apps.youtube.music", "com.spotify.music", "deezer.android.app",
        "com.apple.android.music", "com.soundcloud.android",
    )

    fun open(context: Context): Boolean {
        val pm = context.packageManager
        val intent = players.firstNotNullOfOrNull { pm.getLaunchIntentForPackage(it) } ?: return false
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return true
    }
}

enum class Net { Wifi, Cell, Offline }

/** Current transport, from NetworkCapabilities; needs only the normal ACCESS_NETWORK_STATE. */
@Composable
fun rememberNet(): Net {
    val context = androidx.compose.ui.platform.LocalContext.current
    val cm = remember { context.getSystemService(android.net.ConnectivityManager::class.java) }
    fun read(): Net {
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return Net.Offline
        return when {
            caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) -> Net.Wifi
            caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR) -> Net.Cell
            else -> Net.Offline
        }
    }
    var net by remember { mutableStateOf(read()) }
    DisposableEffect(Unit) {
        val cb = object : android.net.ConnectivityManager.NetworkCallback() {
            override fun onAvailable(n: android.net.Network) { net = read() }
            override fun onLost(n: android.net.Network) { net = read() }
            override fun onCapabilitiesChanged(n: android.net.Network, c: android.net.NetworkCapabilities) { net = read() }
        }
        cm.registerDefaultNetworkCallback(cb)
        onDispose { cm.unregisterNetworkCallback(cb) }
    }
    return net
}

/** Hairline status glyphs drawn in ink, so the bar's tray matches the rest of the print. */
@Composable
fun NetGlyph(net: Net) {
    val ink = T.ink; val faint = T.inkFaint
    androidx.compose.foundation.Canvas(Modifier.size(18.dp).testTag("net:${net.name}")) {
        val w = size.width; val h = size.height; val sw = 1.6.dp.toPx()
        when (net) {
            Net.Wifi -> {
                val c = androidx.compose.ui.geometry.Offset(w / 2, h * 0.86f)
                for (i in 1..3) {
                    val r = h * 0.26f * i
                    drawArc(ink, 225f, 90f, false, androidx.compose.ui.geometry.Offset(c.x - r, c.y - r),
                        androidx.compose.ui.geometry.Size(r * 2, r * 2), style = Stroke(sw, cap = androidx.compose.ui.graphics.StrokeCap.Round))
                }
                drawCircle(ink, sw, c)
            }
            Net.Cell -> for (i in 0..3) {
                val bh = h * (0.3f + 0.22f * i)
                drawLine(ink, androidx.compose.ui.geometry.Offset(w * (0.15f + 0.23f * i), h * 0.9f),
                    androidx.compose.ui.geometry.Offset(w * (0.15f + 0.23f * i), h * 0.9f - bh), sw * 1.4f)
            }
            Net.Offline -> {
                drawLine(faint, androidx.compose.ui.geometry.Offset(w * 0.2f, h * 0.2f), androidx.compose.ui.geometry.Offset(w * 0.8f, h * 0.8f), sw)
                drawLine(faint, androidx.compose.ui.geometry.Offset(w * 0.8f, h * 0.2f), androidx.compose.ui.geometry.Offset(w * 0.2f, h * 0.8f), sw)
            }
        }
    }
}

@Composable
fun BatteryGlyph(level: Int, charging: Boolean) {
    val ink = T.ink; val fill = if (level <= 15) Tokens.alert else T.ink; val line = T.lineStrong
    androidx.compose.foundation.Canvas(Modifier.width(24.dp).height(12.dp).testTag("battery")) {
        val sw = 1.dp.toPx(); val body = size.width - 3.dp.toPx()
        drawRoundRect(line, size = androidx.compose.ui.geometry.Size(body, size.height),
            cornerRadius = androidx.compose.ui.geometry.CornerRadius(2.dp.toPx()), style = Stroke(sw))
        drawRect(ink, androidx.compose.ui.geometry.Offset(body + sw, size.height * 0.3f), androidx.compose.ui.geometry.Size(2.dp.toPx() - sw, size.height * 0.4f))
        val pad = 2.dp.toPx()
        drawRect(fill, androidx.compose.ui.geometry.Offset(pad, pad), androidx.compose.ui.geometry.Size((body - pad * 2) * level / 100f, size.height - pad * 2))
        if (charging) drawLine(androidx.compose.ui.graphics.Color.Black, androidx.compose.ui.geometry.Offset(body * 0.55f, pad),
            androidx.compose.ui.geometry.Offset(body * 0.4f, size.height - pad), sw * 1.5f)
    }
}

/**
 * The computer's Hyprland workspaces, live. The active one is a bone plate (emphasis is
 * inversion). Tapping a workspace opens it as a live view that leaves the computer's monitor
 * alone; holding it switches the monitor itself. A workspace the monitor is showing cannot be
 * viewed (Hyprland shows one workspace on one output), so it is marked PC and a tap does nothing.
 */
@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
private fun DesktopPage(link: DesktopLink, onView: (Int) -> Unit) {
    Row(verticalAlignment = Alignment.Bottom, modifier = Modifier.testTag("desktop")) {
        Text("Desktop", color = T.ink, fontFamily = Type.display, fontSize = Type.fValue)
        Spacer(Modifier.width(Geo.s2))
        Text(link.host.uppercase(), color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fSmall,
            maxLines = 1, modifier = Modifier.padding(bottom = 4.dp).weight(1f))
        Eyebrow("USB", T.inkFaint)
    }
    Text("TAP · VIEW      HOLD · SWITCH PC", color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fTiny, letterSpacing = Type.trackLabel)
    Spacer(Modifier.height(Geo.s2))
    androidx.compose.foundation.lazy.grid.LazyVerticalGrid(androidx.compose.foundation.lazy.grid.GridCells.Fixed(3),
        horizontalArrangement = Arrangement.spacedBy(Geo.s2), verticalArrangement = Arrangement.spacedBy(Geo.s2)) {
        items(link.workspaces.size, key = { link.workspaces[it].id }) { i ->
            val w = link.workspaces[i]
            val ink = if (w.active) T.inkOnBone else T.ink
            val faint = if (w.active) T.inkOnBone.copy(alpha = 0.7f) else T.inkFaint
            Column(Modifier.fillMaxWidth().height(72.dp).clip(RoundedCornerShape(Geo.radius))
                .background(if (w.active) T.bone else Color.Transparent)
                .border(Geo.border, if (w.active) T.bone else T.line, RoundedCornerShape(Geo.radius))
                .combinedClickable(onClick = { onView(w.id) }, onLongClick = { link.switchTo(w.id) })
                .padding(Geo.s2).testTag("ws:${w.id}")) {
                Row(verticalAlignment = Alignment.Bottom) {
                    Text(w.name, color = ink, fontFamily = Type.display, fontSize = 20.sp, lineHeight = Type.fValue)
                    Spacer(Modifier.weight(1f))
                    Text(if (w.busy) "PC · ${w.windows.size}" else "${w.windows.size}", color = faint, fontFamily = Type.mono, fontSize = Type.fMicro)
                }
                // Window classes, deduplicated: "kitty ×3" reads faster than three kitty lines.
                val classes = w.windows.groupingBy { it.cls }.eachCount().entries.sortedByDescending { it.value }
                classes.take(2).forEach { (cls, n) ->
                    Text(if (n > 1) "$cls ×$n" else cls, color = faint, fontFamily = Type.mono, fontSize = Type.fMicro, lineHeight = Type.fSmall, maxLines = 1)
                }
                if (classes.size > 2) Text("+${classes.size - 2} more", color = faint, fontFamily = Type.mono, fontSize = Type.fTiny)
                if (w.windows.isEmpty()) Text("empty", color = faint, fontFamily = Type.mono, fontSize = Type.fMicro)
            }
        }
    }
}
