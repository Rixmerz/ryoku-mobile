package com.rixmerz.ryokumobile

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * One task on one day. The fields mirror the Ryoku desktop's events.json
 * (ryoku/shell/quickshell/shell/services/lib/events.js: id, date, endDate, time, endTime, text),
 * plus `done`, which the desktop model passes through untouched. Keeping the shape identical means
 * syncing with the desktop calendar later is copying one file, not translating between formats.
 */
data class Task(val id: Int, val date: String, val time: String, val text: String, val done: Boolean)

/** Daily tasks, stored as a JSON array in the app's files dir, in the desktop's format. */
class Tasks(context: Context) {
    private val file = File(context.filesDir, "events.json")

    var all by mutableStateOf(load()); private set

    fun forDate(date: String) = all.filter { it.date == date }.sortedWith(compareBy({ it.done }, { it.time.ifEmpty { "99" } }, { it.id }))

    /** (total, done) for a day; the calendar marks days that have any. */
    fun countFor(date: String): Pair<Int, Int> = all.filter { it.date == date }.let { it.size to it.count(Task::done) }

    /** Adds from one typed line; a leading HH:MM becomes the time, as on the desktop widget. */
    fun add(date: String, raw: String): Boolean {
        val line = raw.trim()
        if (line.isEmpty()) return false
        val m = Regex("""^(\d{1,2}):(\d{2})\s+(.+)$""").find(line)
        val (time, text) = if (m != null) "%02d:%s".format(m.groupValues[1].toInt(), m.groupValues[2]) to m.groupValues[3] else "" to line
        save(all + Task((all.maxOfOrNull { it.id } ?: 0) + 1, date, time, text, false))
        return true
    }

    fun toggle(id: Int) = save(all.map { if (it.id == id) it.copy(done = !it.done) else it })

    fun remove(id: Int) = save(all.filterNot { it.id == id })

    private fun save(next: List<Task>) {
        all = next
        val arr = JSONArray()
        next.forEach {
            arr.put(JSONObject().put("id", it.id).put("date", it.date).put("endDate", "").put("time", it.time)
                .put("endTime", "").put("text", it.text).put("done", it.done))
        }
        // Write then rename, so a crash mid-write never leaves a truncated file behind.
        val tmp = File(file.parentFile, "events.json.tmp")
        tmp.writeText(arr.toString())
        tmp.renameTo(file)
    }

    private fun load(): List<Task> = try {
        val arr = JSONArray(file.readText())
        List(arr.length()) { i ->
            val o = arr.getJSONObject(i)
            Task(o.getInt("id"), o.getString("date"), o.optString("time"), o.optString("text"), o.optBoolean("done"))
        }
    } catch (_: Exception) { emptyList() }

    companion object {
        fun key(c: java.util.Calendar) =
            "%04d-%02d-%02d".format(c.get(java.util.Calendar.YEAR), c.get(java.util.Calendar.MONTH) + 1, c.get(java.util.Calendar.DAY_OF_MONTH))
    }
}
