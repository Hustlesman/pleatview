plugins {
    alias(libs.plugins.android.application)
}

// Release signing is optional: provide a keystore through Gradle properties or
// environment variables, otherwise release builds fall back to the debug key so
// they can still be side-loaded.
fun signingValue(name: String): String? =
    providers.gradleProperty("pleatview.$name").orNull
        ?: System.getenv("PLEATVIEW_" + name.uppercase())

// "Support PleatView" in the menu: a QR code for the donation page, in every build.
val donateUrl = "https://pleatview.hustlesman.com/donate"

android {
    namespace = "com.hustlesman.pleatview"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.hustlesman.pleatview"
        minSdk = 24
        targetSdk = 36
        versionCode = 21
        versionName = "1.0.18"
        buildConfigField("String", "DONATE_URL", "\"$donateUrl\"")
    }

    signingConfigs {
        val keystore = signingValue("keystore")
        if (keystore != null) {
            create("release") {
                storeFile = file(keystore)
                storePassword = signingValue("keystore_password")
                keyAlias = signingValue("key_alias")
                keyPassword = signingValue("key_password")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    // Lists the res/values-<lang> translations as the app's languages, so Android 13+ offers
    // PleatView's own language in its app settings (English is res/resources.properties).
    androidResources {
        generateLocaleConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation(libs.androidx.core)
    implementation(libs.androidx.webkit)
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.exoplayer.hls)
    implementation(libs.media3.exoplayer.dash)
    implementation(libs.media3.exoplayer.rtsp)
    implementation(libs.media3.ui)
    implementation(libs.qrcodegen)
    constraints {
        implementation(libs.guava) { because("CVE-2026-102554, in the version Media3 asks for") }
    }

    testImplementation(libs.junit)
    testImplementation(libs.json)
}
