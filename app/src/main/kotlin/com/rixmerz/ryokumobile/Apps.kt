package com.rixmerz.ryokumobile

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.LauncherApps
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.AdaptiveIconDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.os.Process
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap

enum class Category(val label: String) {
    Finance("Finance"), AI("AI"), Social("Social"), Work("Work"), Learning("Learning"), Media("Media"), Games("Games"),
    Food("Food"), Shopping("Shopping"), Travel("Travel"), Tools("Tools"), Other("Other")
}

data class AppEntry(
    val label: String,
    val packageName: String,
    val component: ComponentName,
    val icon: ImageBitmap,
    /** The adaptive icon's monochrome layer (API 33+ themed icons), or null when the app ships none. */
    val mono: ImageBitmap?,
    val category: Category,
    /** False for system apps, which cannot be uninstalled, so the menu never offers it. */
    val removable: Boolean,
)

// Many apps leave ApplicationInfo.category undefined, so fall back to package-name keywords.
// Order matters: the first list with a match wins, so the narrow, high-stakes groups go first.
private val keywordCategories = listOf(
    // Android has no finance category, so this list is all there is: banks (incl. Chilean ones),
    // wallets and brokers. First, so a bank's pay app never lands in Social or Tools.
    Category.Finance to listOf("bank", "banco", "bci.", "santander", "itau", "scotiabank", "bice", "coopeuch",
        "sbpay", "mercadopago", "paypal", "walletnfcrel", "tenpo", "fintual", "revolut", "nubank", "wise",
        "binance", "coinbase", "finance", "invest", "broker"),
    Category.AI to listOf("openai", "chatgpt", "anthropic", "claude", "gemini", "bard", "copilot", "perplexity",
        "deepseek", "grok", "x.ai", "poe", "mistral", "character.ai", "meta.ai"),
    Category.Social to listOf("whatsapp", "telegram", "instagram", "facebook", "twitter", "messag", "discord", "tiktok",
        "musically", "signal", "reddit", "pinterest", "linkedin", "truecaller", "memedroid", "contacts", "dialer",
        "phone", "gmail", "mail"),
    // Before Media, so a meeting app ("zoom.videomeetings") is not filed as video.
    Category.Work to listOf("docs", "drive", "office", "calendar", "notes", "keep", "slack", "teams", "sheets", "zoom",
        "jira", "atlassian", "github", "browser", "chrome", "brave", "firefox"),
    Category.Learning to listOf("duolingo", "hellochinese", "inacap", "classroom", "coursera", "udemy", "khan", "quizlet"),
    Category.Media to listOf("camera", "photo", "gallery", "music", "youtube", "spotify", "netflix", "video", "player",
        "podcast", "instashot", "ibispaint", "songsterr", "frameo", "michv", "twitch", "disney", "hbo"),
    Category.Games to listOf("game", "play.games", "roblox", "callofduty", "chess", "jetpack", "dungeon", "pinturillo",
        "weplay", "imposter", "steamlink", "manabox", "arcadepad"),
    Category.Food to listOf("rappi", "pedidosya", "eats", "burgerking", "kfc", "doggis", "mcdonald", "starbucks", "didifood"),
    Category.Shopping to listOf("aliexpress", "alibaba", "temu", "mercadolibre", "falabella", "amazon", "shein", "ripley.app"),
    Category.Travel to listOf("waze", "maps", "ubercab", "uber", "metro", "pasajebus", "wiu", "cabify", "didi", "airbnb", "booking"),
    // Specific names only: a bare "android" or "google" would swallow half the store.
    Category.Tools to listOf("settings", "deskclock", "clock", "calc", "files", "filemanager", "stk", "safety", "vending",
        "speedtest", "authenticator", "pass", "verifier", "contactkeys", "compass", "translate", "sosafe", "wom",
        "nubia", "zte", "proton"),
)

private fun categorize(info: ApplicationInfo): Category {
    when (info.category) {
        ApplicationInfo.CATEGORY_GAME -> return Category.Games
        ApplicationInfo.CATEGORY_AUDIO, ApplicationInfo.CATEGORY_VIDEO, ApplicationInfo.CATEGORY_IMAGE -> return Category.Media
        ApplicationInfo.CATEGORY_SOCIAL, ApplicationInfo.CATEGORY_NEWS -> return Category.Social
        ApplicationInfo.CATEGORY_PRODUCTIVITY -> return Category.Work
        ApplicationInfo.CATEGORY_MAPS -> return Category.Travel
    }
    val pkg = info.packageName.lowercase()
    return keywordCategories.firstOrNull { (_, words) -> words.any { it in pkg } }?.first ?: Category.Other
}

/** Loads every launchable activity for the current user, sorted by label. */
fun loadApps(context: Context): List<AppEntry> {
    val launcherApps = context.getSystemService(LauncherApps::class.java)
    val iconPx = (56 * context.resources.displayMetrics.density).toInt()
    return launcherApps.getActivityList(null, Process.myUserHandle())
        .filter { it.applicationInfo.packageName != context.packageName }
        .map { info ->
            val drawable = info.getBadgedIcon(0)
            val mono = if (Build.VERSION.SDK_INT >= 33) (drawable as? AdaptiveIconDrawable)?.monochrome else null
            AppEntry(info.label.toString(), info.applicationInfo.packageName, info.componentName,
                render(drawable, iconPx), mono?.let { render(it, iconPx, zoom = 1.45f) }, categorize(info.applicationInfo),
                info.applicationInfo.flags and ApplicationInfo.FLAG_SYSTEM == 0)
        }
        .sortedBy { it.label.lowercase() }
}

// Adaptive icons and their layers have no intrinsic bitmap, so render them at a fixed size.
// A monochrome layer is drawn on the 108dp adaptive canvas with art only in the inner 66dp,
// so it is scaled up to fill the plate instead of sitting tiny in its middle.
private fun render(d: Drawable, px: Int, zoom: Float = 1f): ImageBitmap {
    val bmp = Bitmap.createBitmap(px, px, Bitmap.Config.ARGB_8888)
    val inset = ((zoom - 1f) * px / 2).toInt()
    d.setBounds(-inset, -inset, px + inset, px + inset)
    d.draw(Canvas(bmp))
    return bmp.asImageBitmap()
}

fun launch(context: Context, app: AppEntry) {
    val intent = Intent(Intent.ACTION_MAIN)
        .addCategory(Intent.CATEGORY_LAUNCHER)
        .setComponent(app.component)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
    context.startActivity(intent)
}
