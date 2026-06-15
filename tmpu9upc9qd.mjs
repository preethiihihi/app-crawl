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
        await driver.pause(1000);

        // Step 1: Open dashboard
        console.log('Step 1: Open dashboard');
        const navMenu = await driver.$('~Open navigation menu');
        await navMenu.waitForDisplayed({ timeout: 5000 });
        await navMenu.click();
        await driver.pause(1500);

        // Step 2: Open service requests
        console.log('Step 2: Open service requests');
        const serviceRequests = await driver.$('~Service Requests');
        await serviceRequests.waitForDisplayed({ timeout: 5000 });
        await serviceRequests.click();
        await driver.pause(1500);

        // Step 3: Search for TSR #920
        console.log('Step 3: Search for TSR #920');
        const searchBox = await driver.$('~Search');
        await searchBox.waitForDisplayed({ timeout: 5000 });
        await searchBox.click();
        await searchBox.setValue('TSR #920');
        await driver.pause(1000);
        const searchResult = await driver.$('android=new UiSelector().className("android.widget.TextView").textContains("TSR #920")');
        await searchResult.waitForDisplayed({ timeout: 5000 });

        // Step 4: Open and view the full details of SR
        console.log('Step 4: Open and view the full details of SR');
        const tsrCardTemplate = await driver.$('android=new UiSelector().className("android.view.View").instance(0)');
        await tsrCardTemplate.waitForDisplayed({ timeout: 5000 });
        await tsrCardTemplate.click();
        await driver.pause(1500);

    } catch (error) {
        console.error('Automation execution failure encountered:', error);
    } finally {
        await driver.deleteSession();
        console.log('Appium session ended.');
    }
})();