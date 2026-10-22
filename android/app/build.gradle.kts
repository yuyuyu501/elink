plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.yuyuyu501.elink"
    compileSdk = 35
    defaultConfig {
        applicationId = "io.github.yuyuyu501.elink"
        minSdk = 30
        targetSdk = 35
        versionCode = 503
        versionName = "0.5.3"
        ndk { abiFilters += "arm64-v8a" }
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    signingConfigs {
        create("preview") {
            val keyPath = System.getenv("ELINK_ANDROID_KEYSTORE")
            if (keyPath != null) {
                storeFile = file(keyPath)
                storePassword = System.getenv("ELINK_ANDROID_STORE_PASSWORD")
                keyAlias = System.getenv("ELINK_ANDROID_KEY_ALIAS") ?: "elink"
                keyPassword = System.getenv("ELINK_ANDROID_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("preview")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    sourceSets["main"].assets.srcDir("../../licenses/android")
    packaging { resources.excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE*", "META-INF/NOTICE*") }
}

dependencies {
    implementation("io.github.webrtc-sdk:android:125.6422.07")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.1")
    testImplementation("junit:junit:4.13.2")
}
