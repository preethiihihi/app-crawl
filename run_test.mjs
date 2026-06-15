import { remote } from 'webdriverio';

(async () => {
    const caps = {
        platformName: 'Android',
        'appium:automationName': 'UiAutomator2',
        'appium:deviceName': 'emulator-5554',
        'appium:appPackage': 'com.kuberproject',
        'appium:appActivity': '.MainActivity',
        'appium:newCommandTimeout': 240,
        'appium:noReset': true
    };

    const driver = await remote({
        hostname: '127.0.0.1',
        port: 4723,
        path: '/',
        capabilities: caps
    });

    try {
        console.log('Successfully connected to the active application session...');
        await driver.pause(2000);

        // Terminate and reactivate app to ensure starting from dashboard home state
        console.log('Re-launching application to start from dashboard home state...');
        await driver.terminateApp('com.kuberproject');
        await driver.pause(1500);
        await driver.activateApp('com.kuberproject');
        await driver.pause(3000);

        // Step 1: Open dashboard / Verify dashboard
        console.log('Step 1: Open dashboard / Verify dashboard');
        const dashboardTitle = await driver.$('android=new UiSelector().description("Dashboard")');
        await dashboardTitle.waitForDisplayed({ timeout: 10000 });
        console.log('Dashboard screen is displayed.');

        // Step 2: Open service requests
        console.log('Step 2: Open service requests');
        const navMenu = await driver.$('~Open navigation menu');
        await navMenu.waitForDisplayed({ timeout: 5000 });
        await navMenu.click();
        await driver.pause(1500);

        const serviceRequestsMenu = await driver.$('~Service Requests');
        await serviceRequestsMenu.waitForDisplayed({ timeout: 5000 });
        await serviceRequestsMenu.click();
        await driver.pause(2000);

        // Step 3: Search for 920
        console.log('Step 3: Search for 920');
        const searchBox = await driver.$('android=new UiSelector().className("android.widget.EditText")');
        await searchBox.waitForDisplayed({ timeout: 5000 });
        await searchBox.click();
        await driver.pause(500);
        await searchBox.setValue('920');
        console.log('Typed 920 in search field.');
        await driver.pause(2000);

        // Step 4: Open and view the full details of SR
        console.log('Step 4: Open and view the full details of SR');
        const targetCard = await driver.$('android=new UiSelector().descriptionContains("TSR #920")');
        await targetCard.waitForDisplayed({ timeout: 8000 });
        await targetCard.click();
        console.log('Clicked on TSR #920 card');
        await driver.pause(3000);

        // Verify details
        const detailsContainer = await driver.$('android=new UiSelector().descriptionContains("TSR #920")');
        const isDetailsVisible = await detailsContainer.isDisplayed();
        console.log(`SR Details View Verification: ${isDetailsVisible ? 'SUCCESS' : 'FAILED'}`);

    } catch (error) {
        console.error('Automation execution failure encountered:', error);
    } finally {
        await driver.deleteSession();
        console.log('Appium session ended.');
    }
})();
