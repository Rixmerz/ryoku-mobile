package com.rixmerz.ryokumobile

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Launcher settings, dock pins and launch counts, backed by SharedPreferences and observable from Compose. */
class LauncherPrefs(context: Context) {
    private val prefs = context.getSharedPreferences("ryoku", Context.MODE_PRIVATE)

    var scheme by mutableStateOf(prefs.getString("scheme", signature.id)!!); private set
    var wallpaper by mutableStateOf(wallpaperById(prefs.getString("wallpaper2", "")!!)); private set
    var face by mutableStateOf(faceById(prefs.getString("face", "")!!)); private set
    var calendar by mutableStateOf(prefs.getBoolean("calendar", true)); private set
    var columns by mutableIntStateOf(prefs.getInt("columns", 4)); private set
    var labels by mutableStateOf(prefs.getBoolean("labels", true)); private set
    var inkIcons by mutableStateOf(prefs.getBoolean("inkIcons", true)); private set
    var pins by mutableStateOf(split(prefs.getString("pins", ""))); private set
    private var usage by mutableStateOf(parseUsage(prefs.getString("usage", "")))

    fun chooseScheme(id: String) { scheme = id; prefs.edit().putString("scheme", id).apply() }
    fun chooseWallpaper(w: Wallpaper) { wallpaper = w; prefs.edit().putString("wallpaper2", w.name).apply() }
    fun chooseFace(f: Face) { face = f; prefs.edit().putString("face", f.name).apply() }
    fun chooseCalendar(on: Boolean) { calendar = on; prefs.edit().putBoolean("calendar", on).apply() }
    fun chooseColumns(n: Int) { columns = n; prefs.edit().putInt("columns", n).apply() }
    fun chooseLabels(on: Boolean) { labels = on; prefs.edit().putBoolean("labels", on).apply() }

    fun chooseInkIcons(on: Boolean) { inkIcons = on; prefs.edit().putBoolean("inkIcons", on).apply() }

    fun isPinned(pkg: String) = pkg in pins

    fun togglePin(pkg: String) {
        val next = pins.toMutableList()
        if (!next.remove(pkg)) {
            if (next.size >= MAX_PINS) next.removeAt(0)
            next.add(pkg)
        }
        pins = next
        prefs.edit().putString("pins", next.joinToString(",")).apply()
    }

    fun recordLaunch(pkg: String) {
        usage = usage + (pkg to (usage[pkg] ?: 0) + 1)
        prefs.edit().putString("usage", usage.entries.joinToString(";") { "${it.key}=${it.value}" }).apply()
    }

    /** Most launched packages first; empty until the user has opened something. */
    fun frequent(limit: Int): List<String> =
        usage.entries.sortedByDescending { it.value }.take(limit).map { it.key }

    private fun split(s: String?) = s.orEmpty().split(',').filter { it.isNotBlank() }

    private fun parseUsage(s: String?): Map<String, Int> =
        s.orEmpty().split(';').mapNotNull {
            val (k, v) = it.split('=').takeIf { p -> p.size == 2 } ?: return@mapNotNull null
            v.toIntOrNull()?.let { n -> k to n }
        }.toMap()

    companion object { const val MAX_PINS = 5 }
}
