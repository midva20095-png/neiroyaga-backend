import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val keystoreProperties = Properties().apply {
    val file = rootProject.file("keystore.properties")
    if (file.exists()) {
        file.inputStream().use { load(it) }
    }
}

android {
    namespace = "ru.babayagaland.app"
    compileSdk = 35

    defaultConfig {
        minSdk = 26
        targetSdk = 35
    }

    flavorDimensions += "kind"
    productFlavors {
        create("site") {
            dimension = "kind"
            applicationId = "ru.babayagaland.app"
            versionCode = 3
            versionName = "1.2.0"
            resValue("string", "app_name", "НейроЯга")
            resValue("string", "home_url", "https://babayagaland.ru/")
        }
        create("adminbot") {
            dimension = "kind"
            applicationId = "ru.babayagaland.adminbot"
            versionCode = 1
            versionName = "1.0.0"
            resValue("string", "app_name", "Админ бота")
            resValue("string", "home_url", "https://neirozhiga-ai-telegram-bot-production-7ae5.up.railway.app/admin")
        }
        create("adminsite") {
            dimension = "kind"
            applicationId = "ru.babayagaland.adminsite"
            versionCode = 1
            versionName = "1.0.0"
            resValue("string", "app_name", "Админ сайта")
            resValue("string", "home_url", "https://neiroyaga-backend-production.up.railway.app/admin")
        }
    }

    signingConfigs {
        create("release") {
            if (keystoreProperties.isNotEmpty()) {
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            isShrinkResources = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        jniLibs {
            useLegacyPackaging = false
        }
        resources {
            excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE", "META-INF/LICENSE.txt", "META-INF/NOTICE", "META-INF/NOTICE.txt")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.core:core-splashscreen:1.0.1")
}
