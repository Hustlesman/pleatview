package com.hustlesman.pleatview

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

class PageStepsTest {
    /** The examples the renderer and the Linux app read too. */
    private val examples = JSONObject(File("../../tests/fixtures/page-steps.json").readText())

    private fun step(json: JSONObject): PageSteps.Step {
        val numbers = json.optJSONArray("numbers") ?: JSONArray()
        return PageSteps.Step(
            kind = json.getString("kind"),
            text = json.optString("text"),
            numbers = List(numbers.length()) { numbers.getDouble(it) },
            css = json.optString("css"),
        )
    }

    @Test
    fun `the shared examples give the same page and steps`() {
        val valid = examples.getJSONArray("valid")
        assertTrue(valid.length() >= 5)
        for (i in 0 until valid.length()) {
            val example = valid.getJSONObject(i)
            val steps = example.getJSONArray("steps")
            val (page, parsed) = PageSteps.split(example.getString("address"))
            assertEquals(example.getString("address"), example.getString("page"), page)
            assertEquals(List(steps.length()) { step(steps.getJSONObject(it)) }, parsed)
        }
    }

    @Test
    fun `wrong steps are refused with a reason`() {
        val invalid = examples.getJSONArray("invalid")
        for (i in 0 until invalid.length()) {
            val address = invalid.getString(i)
            try {
                PageSteps.split(address)
                fail("expected a refusal for $address")
            } catch (e: IllegalArgumentException) {
                assertTrue(address, !e.message.isNullOrBlank())
            }
        }
    }

    @Test
    fun `a plus in the steps stays a plus`() {
        val (_, steps) = PageSteps.split("https://a.example/#pleatview=click:css=li+li%20a")
        assertEquals(listOf(PageSteps.Step("click", css = "li+li a")), steps)
    }
}
