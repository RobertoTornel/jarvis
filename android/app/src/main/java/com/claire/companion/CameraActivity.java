package com.claire.companion;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageCapture;
import androidx.camera.core.ImageCaptureException;
import androidx.camera.core.Preview;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.google.common.util.concurrent.ListenableFuture;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Camera at will: one photo, taken on purpose, sent to Claire, then forgotten.
 *
 * <p>CameraX rather than Camera2 for one specific reason: CameraX decides for itself
 * which physical lens, which resolution and which rotation are right, on a phone this
 * app cannot test exhaustively. Camera2 would mean writing that decision here and
 * getting it wrong on some MIUI build.
 *
 * <p>Two ways in, one screen:
 *
 * <ul>
 *   <li>from the app's <b>Photo</b> button — a preview, a shutter, a review, then send
 *       or retake, so nothing leaves the phone that the owner did not mean to send;
 *   <li>from the notification's <b>Photo</b> action with {@link #EXTRA_AUTO} — the
 *       camera opens and captures after a short settle, without a second tap, because
 *       the whole point is that this works with the phone in one hand. The captured
 *       frame is still shown for a moment before it is sent.
 * </ul>
 *
 * <p>Nothing is written anywhere but the app's own cache directory, and that file is
 * deleted as soon as the upload finishes, succeeds or fails. The laptop keeps the
 * photo, because Claire's vision seam needs a path — the phone does not need to keep a
 * second copy of the owner's camera roll.
 */
public class CameraActivity extends AppCompatActivity {

    /** Capture and send without waiting for a shutter tap. */
    public static final String EXTRA_AUTO = "auto";

    /** Longest edge of the JPEG that is uploaded. */
    private static final int MAX_EDGE_PX = 1600;

    /** For logcat: the whole reason a photo failed, not just the screen's summary. */
    private static final String TAG = "ClaireCamera";

    /** How long the auto path waits for focus and exposure to settle. */
    private static final long AUTO_SETTLE_MS = 1200L;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    private ClairePrefs prefs;
    private PreviewView previewView;
    private TextView status;
    private Button shutter;
    private Button send;
    private Button retake;
    private ImageCapture imageCapture;
    private boolean auto;
    private File captured;
    private boolean busy;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_camera);

        prefs = new ClairePrefs(this);
        auto = getIntent().getBooleanExtra(EXTRA_AUTO, false);
        com.google.android.material.appbar.MaterialToolbar toolbar =
                findViewById(R.id.camera_toolbar);
        toolbar.setNavigationOnClickListener(view -> finish());
        previewView = findViewById(R.id.preview);
        status = findViewById(R.id.camera_status);
        shutter = findViewById(R.id.shutter);
        send = findViewById(R.id.send);
        retake = findViewById(R.id.retake);

        shutter.setOnClickListener(view -> capture());
        retake.setOnClickListener(view -> {
            deleteCaptured();
            showLive();
        });
        send.setOnClickListener(view -> upload());

        keepControlsOutOfTheGestureArea();

        if (checkSelfPermission(Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            status.setText(R.string.camera_no_permission);
            ActivityCompat.requestPermissions(this,
                    new String[] {Manifest.permission.CAMERA}, 61);
            return;
        }
        startCamera();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != 61) {
            return;
        }
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startCamera();
        } else {
            Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "camera permission was refused, so no photo was taken");
            finish();
        }
    }

    private void startCamera() {
        ListenableFuture<ProcessCameraProvider> future =
                ProcessCameraProvider.getInstance(this);
        future.addListener(() -> {
            try {
                ProcessCameraProvider provider = future.get();
                Preview preview = new Preview.Builder().build();
                preview.setSurfaceProvider(previewView.getSurfaceProvider());
                imageCapture = new ImageCapture.Builder()
                        .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                        .build();
                provider.unbindAll();
                provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA,
                        preview, imageCapture);
                status.setText(R.string.camera_title);
                if (auto) {
                    shutter.setEnabled(false);
                    main.postDelayed(this::capture, AUTO_SETTLE_MS);
                }
            } catch (Exception error) {
                status.setText(getString(R.string.camera_failed,
                        error.getClass().getSimpleName() + ": " + error.getMessage()));
                // Stay tappable. Disabling the shutter here made the retry in
                // capture() unreachable: that path exists to say "the camera is
                // still opening; tap again in a moment" and call startCamera()
                // again, and a button the owner cannot press is the "i cant tap
                // it" symptom this screen exists to remove. showLive() is the
                // only other place that re-enables it, and a bind failure never
                // reaches it, so a disabled shutter stranded them for good.
                shutter.setEnabled(true);
            }
        }, ContextCompat.getMainExecutor(this));
    }

    private void capture() {
        if (busy) {
            // Say so rather than ignoring the tap: a button that does nothing at all
            // is indistinguishable from one that is broken.
            status.setText(R.string.camera_busy);
            return;
        }
        if (imageCapture == null) {
            // The camera had not finished binding when the finger arrived. This used to
            // be a silent `return`, which is exactly what "i cant tap it" looks like
            // from the outside.
            status.setText(R.string.camera_not_ready);
            startCamera();
            return;
        }
        busy = true;
        shutter.setEnabled(false);
        status.setText(R.string.camera_capturing);
        final File target;
        try {
            target = File.createTempFile("claire-photo", ".jpg", getCacheDir());
        } catch (Exception error) {
            status.setText("could not make room for the photo: " + error.getMessage());
            busy = false;
            return;
        }
        ImageCapture.OutputFileOptions options =
                new ImageCapture.OutputFileOptions.Builder(target).build();
        imageCapture.takePicture(options, ContextCompat.getMainExecutor(this),
                new ImageCapture.OnImageSavedCallback() {
                    @Override
                    public void onImageSaved(@NonNull ImageCapture.OutputFileResults results) {
                        captured = target;
                        busy = false;
                        if (auto || prefs.autoSendPhoto()) {
                            status.setText(R.string.camera_sending);
                            upload();
                        } else {
                            status.setText("Photo ready. Send it to Claire?");
                            shutter.setVisibility(View.GONE);
                            send.setVisibility(View.VISIBLE);
                            retake.setVisibility(View.VISIBLE);
                        }
                    }

                    @Override
                    public void onError(@NonNull ImageCaptureException error) {
                        busy = false;
                        shutter.setEnabled(true);
                        // The owner's report was "photos error out" and the screen used to
                        // say only error.getMessage(), which on MIUI is often just
                        // "Failed to take picture". The numeric CameraX code and the
                        // cause are what make a failure actionable, so they are named
                        // here, put in the conversation, and written to logcat.
                        String detail = describeCaptureFailure(error);
                        Log.e(TAG, "takePicture failed: " + detail, error);
                        status.setText(detail);
                        Conversation.get(CameraActivity.this).add(
                                Conversation.Who.SYSTEM, "the photo could not be taken: " + detail);
                    }
                });
    }

    /** The real reason, not the wrapper's one-line summary. */
    static String describeCaptureFailure(ImageCaptureException error) {
        StringBuilder out = new StringBuilder();
        out.append("CameraX error ").append(error.getImageCaptureError());
        String message = error.getMessage();
        if (message != null && !message.trim().isEmpty()) {
            out.append(" (").append(message.trim()).append(')');
        }
        Throwable cause = error.getCause();
        if (cause != null) {
            out.append(" caused by ").append(cause.getClass().getSimpleName());
            if (cause.getMessage() != null && !cause.getMessage().trim().isEmpty()) {
                out.append(": ").append(cause.getMessage().trim());
            }
        }
        return out.toString();
    }

    /**
     * Lift the controls clear of the system's own edge gesture.
     *
     * <p>The owner's words were "i cant tap it, move the button up". The shutter used
     * to sit 14dp from the bottom of the screen, which is inside the gesture strip on
     * a phone using gesture navigation — the swipe that goes Back eats the tap before
     * the button ever sees it. The strip's height is a platform constant that changes
     * with the navigation mode, so it is measured here rather than guessed at, and the
     * padding is the gesture inset plus the original margin.
     */
    private void keepControlsOutOfTheGestureArea() {
        View controls = findViewById(R.id.camera_controls);
        if (controls == null) {
            return;
        }
        final int base = Math.round(14f * getResources().getDisplayMetrics().density);
        ViewCompat.setOnApplyWindowInsetsListener(controls, (view, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemGestures()
                    | WindowInsetsCompat.Type.navigationBars());
            view.setPadding(view.getPaddingLeft(), view.getPaddingTop(),
                    view.getPaddingRight(), Math.max(base, bars.bottom + base));
            return insets;
        });
        ViewCompat.requestApplyInsets(controls);
    }

    private void showLive() {
        deleteCaptured();
        shutter.setVisibility(View.VISIBLE);
        shutter.setEnabled(true);
        send.setVisibility(View.GONE);
        retake.setVisibility(View.GONE);
        status.setText(R.string.camera_title);
        if (auto) {
            main.postDelayed(this::capture, AUTO_SETTLE_MS);
        }
    }

    private void upload() {
        if (captured == null || !captured.isFile()) {
            status.setText("there is no photo to send");
            return;
        }
        status.setText(R.string.camera_sending);
        send.setEnabled(false);
        final File file = captured;
        final String question = prefs.question();
        worker.execute(() -> {
            byte[] jpeg = shrink(file);
            ClaireClient.Answer answer = ClaireClient.from(prefs).see(jpeg, question);
            delete(file);
            main.post(() -> onUploaded(answer, jpeg.length));
        });
    }

    private void onUploaded(ClaireClient.Answer answer, int bytes) {
        captured = null;
        if (!answer.ok()) {
            String detail = answer.describe("sending the photo");
            if (answer.status == 401) {
                detail = "sending the photo: the laptop refused the token (HTTP 401)";
            }
            Conversation.get(this).add(Conversation.Who.SYSTEM, detail);
            Notifier.alert(this, getString(R.string.app_name), detail);
            status.setText(detail);
            send.setEnabled(true);
            return;
        }
        boolean seen = ClaireClient.booleanField(answer.body, "seen");
        String description = ClaireClient.field(answer.body, "description");
        if (seen && !description.isEmpty()) {
            Conversation.get(this).add(Conversation.Who.PHONE,
                    "sent a photo (" + (bytes / 1024) + " kB)");
            Conversation.get(this).add(Conversation.Who.CLAIRE, description);
            status.setText(description);
        } else {
            String message = ClaireClient.errorMessage(answer.body);
            String remedy = ClaireClient.errorRemedy(answer.body);
            String detail = "Claire did not look at the photo"
                    + (message.isEmpty() ? "" : ": " + message)
                    + (remedy.isEmpty() ? "" : "\n" + remedy);
            Conversation.get(this).add(Conversation.Who.SYSTEM, detail);
            status.setText(detail);
        }
        Toast.makeText(this, R.string.camera_sent, Toast.LENGTH_SHORT).show();
        main.postDelayed(this::finish, 900);
    }

    /**
     * Downscale to {@link #MAX_EDGE_PX} on the long edge.
     *
     * <p>A modern phone camera hands over 4-12 MB per frame; the bridge's body limit is
     * 8 MiB and a vision model does not need more than 1600 px to describe a room. This
     * is done with {@link BitmapFactory} on the upload thread — never on the main
     * thread, which is where a full-resolution decode would be a visible freeze — and
     * if anything about it fails the original bytes are sent rather than nothing.
     */
    private byte[] shrink(File file) {
        try {
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            BitmapFactory.decodeFile(file.getAbsolutePath(), bounds);
            int sample = 1;
            int longest = Math.max(bounds.outWidth, bounds.outHeight);
            while (longest / sample > MAX_EDGE_PX) {
                sample *= 2;
            }
            BitmapFactory.Options decode = new BitmapFactory.Options();
            decode.inSampleSize = sample;
            Bitmap bitmap = BitmapFactory.decodeFile(file.getAbsolutePath(), decode);
            if (bitmap == null) {
                return readFile(file);
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            bitmap.compress(Bitmap.CompressFormat.JPEG, 85, out);
            bitmap.recycle();
            byte[] shrunk = out.toByteArray();
            return shrunk.length > 0 ? shrunk : readFile(file);
        } catch (Exception error) {
            return readFile(file);
        }
    }

    private static byte[] readFile(File file) {
        try {
            byte[] buffer = new byte[(int) file.length()];
            try (java.io.FileInputStream in = new java.io.FileInputStream(file)) {
                int offset = 0;
                while (offset < buffer.length) {
                    int read = in.read(buffer, offset, buffer.length - offset);
                    if (read < 0) {
                        break;
                    }
                    offset += read;
                }
            }
            return buffer;
        } catch (Exception error) {
            return new byte[0];
        }
    }

    private void deleteCaptured() {
        delete(captured);
        captured = null;
    }

    private static void delete(File file) {
        if (file != null && file.isFile() && !file.delete()) {
            // Not worth a message: the cache directory is wiped by the platform.
        }
    }

    @Override
    protected void onDestroy() {
        deleteCaptured();
        worker.shutdownNow();
        main.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
