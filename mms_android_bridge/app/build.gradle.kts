plugins {
    id("com.android.application")
}

android {
    namespace = "cl.aura.mmsbridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "cl.aura.mmsbridge"
        minSdk = 26
        targetSdk = 34
        versionCode = 4
        versionName = "0.4.0-resilient"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
