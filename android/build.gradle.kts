plugins {
    alias(libs.plugins.android.application) apply false
    // Not applied (AGP compiles Kotlin itself): only picks the Kotlin version AGP uses.
    alias(libs.plugins.kotlin.android) apply false
}

// AGP's own tools still ask for library versions with published CVEs. None of them reach
// anything but this project's files, but newer versions cost nothing. Minimums only: a
// newer AGP that asks for more wins.
buildscript {
    dependencies {
        constraints {
            listOf(
                "org.bouncycastle:bcprov-jdk18on:1.86",
                "org.bouncycastle:bcpkix-jdk18on:1.86",
                "org.bouncycastle:bcutil-jdk18on:1.86",
                "org.jdom:jdom2:2.0.6.1",
                "org.bitbucket.b_c:jose4j:0.9.7",
                "org.apache.commons:commons-lang3:3.21.0",
                "com.google.guava:guava:33.8.0-jre",
            ).forEach { classpath(it) }
        }
    }
}
