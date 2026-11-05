package com.rixmerz.ryokumobile

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random

/**
 * Generative wallpapers. Wallpaper is art, the one place colour may live, so each style paints
 * with the active scheme (paper, sun, secondary, tertiary) and retints with it.
 */
enum class Wallpaper(val label: String, val gloss: String) {
    Sun("Sun", "日輪"), Aurora("Aurora", "極光"), Tide("Tide", "潮"), Orbit("Orbit", "軌道"), Paper("Paper", "紙")
}

fun wallpaperById(id: String) = Wallpaper.entries.firstOrNull { it.name == id } ?: Wallpaper.Sun

// Fixed seed: the grain is part of the print, it must not shimmer between frames.
private val grain = Random(7).let { r -> List(1400) { Offset(r.nextFloat(), r.nextFloat()) to r.nextFloat() } }

@Composable
fun LiveWallpaper(wp: Wallpaper, tokens: Tokens, modifier: Modifier = Modifier, animated: Boolean = true) {
    val t = if (animated) {
        val transition = rememberInfiniteTransition(label = "wallpaper")
        val v by transition.animateFloat(0f, (2 * PI).toFloat(),
            infiniteRepeatable(tween(120_000, easing = LinearEasing), RepeatMode.Restart), label = "t")
        v
    } else 1.3f
    val tones = remember(tokens) { listOf(tokens.sun, tokens.secondary, tokens.tertiary) }
    Canvas(modifier) {
        drawRect(tokens.paper)
        when (wp) {
            Wallpaper.Sun -> sun(tokens, t)
            Wallpaper.Aurora -> aurora(tokens.paper, tones, t)
            Wallpaper.Tide -> tide(tokens.paper, tones, t)
            Wallpaper.Orbit -> orbit(tokens, t)
            Wallpaper.Paper -> Unit
        }
        if (wp != Wallpaper.Paper) drawGrain(tokens.ink)
    }
}

/** The house art: a red sun over a hairline horizon, as on the Ryoku desktop. */
private fun DrawScope.sun(k: Tokens, t: Float) {
    val horizon = size.height * 0.74f
    val r = size.width * 0.2f
    val c = Offset(size.width * 0.62f, horizon - r * 0.55f + 6f * sin(t * 2))
    drawCircle(Brush.radialGradient(listOf(k.sun.copy(alpha = 0.22f), Color.Transparent), c, r * 2.6f), r * 2.6f, c)
    drawCircle(k.sun, r, c)
    // Below the horizon the sun is reflected as thinning bars.
    drawRect(k.paper, Offset(0f, horizon), androidx.compose.ui.geometry.Size(size.width, size.height - horizon))
    for (i in 0 until 7) {
        val y = horizon + 18f + i * 22f
        val w = r * (1.6f - i * 0.2f) * (0.92f + 0.08f * sin(t * 3 + i))
        drawLine(k.sun.copy(alpha = 0.55f - i * 0.07f), Offset(c.x - w / 2, y), Offset(c.x + w / 2, y), 3f - i * 0.3f)
    }
    drawLine(k.ink.copy(alpha = 0.42f), Offset(0f, horizon), Offset(size.width, horizon), 1f)
}

private fun DrawScope.aurora(paper: Color, tones: List<Color>, t: Float) {
    tones.forEachIndexed { i, c ->
        // Integer speeds keep the loop seamless when t wraps from 2*PI back to 0.
        val speed = (i + 1).toFloat()
        val center = Offset(size.width * (0.5f + 0.4f * sin(t * speed + i * 2.1f)),
            size.height * (0.32f + 0.2f * cos(t * speed + i * 1.3f) + i * 0.12f))
        val r = size.width * (0.75f + 0.1f * i)
        drawCircle(Brush.radialGradient(listOf(c.copy(alpha = 0.40f), Color.Transparent), center, r), r, center)
    }
    drawRect(Brush.verticalGradient(listOf(Color.Transparent, paper), startY = size.height * 0.55f))
}

private fun DrawScope.tide(paper: Color, tones: List<Color>, t: Float) {
    val layers = tones + tones.first()
    layers.forEachIndexed { i, c ->
        val base = size.height * (0.56f + 0.09f * i)
        val amp = size.height * (0.03f - 0.004f * i)
        val path = Path().apply {
            moveTo(0f, size.height)
            var x = 0f
            while (x <= size.width + 8f) {
                val p = x / size.width * 2f * PI.toFloat()
                lineTo(x, base + amp * sin(p * (1.2f + 0.3f * i) + t * (i % 2 * 2 - 1) + i))
                x += 8f
            }
            lineTo(size.width, size.height); close()
        }
        drawPath(path, androidx.compose.ui.graphics.lerp(paper, c, 0.25f + 0.17f * i))
    }
}

private fun DrawScope.orbit(k: Tokens, t: Float) {
    val center = Offset(size.width * 0.7f, size.height * 0.42f)
    for (i in 1..7) {
        val r = size.width * 0.085f * i
        drawCircle(k.ink.copy(alpha = 0.08f + 0.02f * (7 - i)), r, center, style = Stroke(1f))
        val a = t * (8 - i) * (if (i % 2 == 0) 1 else -1) + i
        val dot = Offset(center.x + r * cos(a), center.y + r * sin(a))
        drawCircle(if (i == 3) k.sun else k.ink.copy(alpha = 0.7f), if (i == 3) 7f else 2.5f, dot)
    }
}

private fun DrawScope.drawGrain(ink: Color) {
    grain.forEach { (p, a) ->
        drawCircle(ink.copy(alpha = 0.05f * a), 1.2f, Offset(p.x * size.width, p.y * size.height))
    }
}
